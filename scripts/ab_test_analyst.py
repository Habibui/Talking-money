#!/usr/bin/env python3
"""
27-28.09.2026 (выходные) — A/B/C-тест Аналитика на трёх моделях
(Sonnet 5 / Opus 5.5 / Fable 5.1) на ОДНОМ и том же входе от Отборщика,
вслепую для автора. Точные требования — менеджер проекта, 26.09.2026,
вечер (см. claude/format-v2-analytical-brief.md в Cowork Project, «Ответ
менеджера проекта (26.09.2026)», пункт 3):

  - Отборщик запускается ОДИН раз, его выход замораживается в файл — все
    три Аналитика получают идентичный вход. Реализовано буквально: первый
    запуск этого скрипта в календарный день вызывает Отборщик и пишет
    результат в ab_test_output/selector_frozen_<дата>.json; ЛЮБОЙ
    повторный запуск в тот же день по умолчанию использует этот файл, а
    не гоняет Отборщик снова (--force-reselect — если это осознанно
    нужно; --input <путь> — явно указать другой замороженный файл).
  - Фактчекер — один и тот же (Opus 5.5, effort=medium, см.
    FACTCHECKER_MODEL/FACTCHECKER_EFFORT ниже) для всех трёх черновиков;
    по каждому фиксируются unsupported_count/any_number_distorted/
    hook_unsupported.
  - effort — ОДИНАКОВЫЙ (medium) у всех трёх участников, включая Sonnet 5
    (поправка менеджера 26.09.2026: НЕ дефолт каждой модели — более
    раннее сообщение автору предлагало выставить Fable 5.1 на его дефолт
    "high", это была ошибка именно того рода, ради которого effort и
    появился как явный параметр).
  - Sonnet 5 в роли Аналитика — с adaptive thinking (не disabled), для
    паритета с тем, как он будет реально работать в проде, если выиграет
    A/B (поправка менеджера 26.09.2026, п.2). Реализовано через
    `thinking_override="adaptive"` у analyst.write_issue() — см.
    src/llm_json.py (седьмой пункт) и src/analyst.py. Отборщик
    (src/selector.py) этот механизм не использует и не должен: там
    thinking остаётся всегда disabled, вопрос паритета с продом там не
    стоит.
  - Вслепую: три черновика сохраняются под метками A/B/C в случайном
    порядке; файл для автора (ab_test_output/for_author_<дата>.md) не
    содержит названий моделей. Соответствие буква<->модель + usage +
    стоимость по факту — в отдельном файле
    (ab_test_output/technical_<дата>.json), который автору до момента
    выбора показывать не нужно.

Критерии выбора (менеджер, 26.09.2026, п.4) — это решение человека, СКРИПТ
их не применяет автоматически: точность (по фактчекеру) → понятность и
хук → реальность связей между сюжетами → при примерном равенстве —
самая дешёвая модель (см. technical_<дата>.json, total_cost_usd_estimate
по факту, а не по прайс-листу вслепую).

Запуск:
  python3 scripts/ab_test_analyst.py
  python3 scripts/ab_test_analyst.py --hours=24
  python3 scripts/ab_test_analyst.py --force-reselect
  python3 scripts/ab_test_analyst.py --input ab_test_output/selector_frozen_2026-09-27.json

Собирать архив (Сборщик) этот скрипт сам не запускает — сделайте это
заранее (scripts/dry_run_v2.py или отдельный вызов src.collector.run()),
как и раньше. ANTHROPIC_API_KEY обязателен — все вызовы реальные, как в
scripts/dry_run_v2.py, мока здесь нет.
"""

import argparse
import json
import logging
import os
import random
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import archive, config, factchecker, analyst, selector, telegram_render

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ab_test_analyst")

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ab_test_output")

# Цены за 1M токенов вход/выход (платформа, 09.2026) — см.
# claude/format-v2-analytical-brief.md, раздел «Модель аналитика».
# Меняются только здесь, если менеджер обновит прайс до воскресенья.
PRICING_PER_1M = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-fable-5-1": (10.0, 50.0),
}

# Три участника A/B. effort ОДИНАКОВЫЙ (medium) у всех трёх — поправка
# менеджера 26.09.2026, см. docstring модуля выше. thinking_override —
# только у Sonnet 5 (паритет с продом); у Opus 5.5/Fable 5.1 не нужен —
# для них llm_json._thinking_config() и так безусловно возвращает
# "adaptive" (thinking у них нельзя выключить в принципе).
PARTICIPANTS = [
    {"label": "sonnet-5", "model": "claude-sonnet-5", "effort": "medium", "thinking_override": "adaptive"},
    {"label": "opus-5.5", "model": "claude-opus-5-5", "effort": "medium", "thinking_override": None},
    {"label": "fable-5.1", "model": "claude-fable-5-1", "effort": "medium", "thinking_override": None},
]

# Фактчекер — один и тот же для всех трёх черновиков (см. бриф: Фактчекер
# сам в сравнении не участвует и навсегда остаётся на Opus 5.5,
# независимо от результата A/B Аналитика).
FACTCHECKER_MODEL = "claude-opus-5-5"


def _cost_usd(model: str, input_tokens, output_tokens):
    """Возвращает оценку стоимости в USD по факту реальных
    input_tokens/output_tokens (не по видимому тексту ответа — thinking-
    токены уже включены в output_tokens, см. src/llm_json.py про то, что
    Anthropic API не отдаёт их отдельным полем), либо None, если usage
    недоступен ("?" из _log_usage при отсутствующем response.usage) или
    модель не из PRICING_PER_1M."""
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    pricing = PRICING_PER_1M.get(model)
    if pricing is None:
        return None
    price_in, price_out = pricing
    return round(input_tokens / 1_000_000 * price_in + output_tokens / 1_000_000 * price_out, 4)


def _freeze_selector_output(hours: int) -> dict:
    """Один реальный вызов Отборщика. Замораживает ровно то подмножество
    заметок, которое реально используется отобранными сюжетами (не весь
    архив за окно) — этого достаточно и Аналитику (notes_by_id для его
    stories), и позже Фактчекеру (source_ids каждого черновика — всегда
    подмножество того же множества). Смысл замораживания в файл, а не
    просто в памяти процесса: вход должен быть идентичным byte-в-byte
    для всех трёх Аналитиков независимо от того, что происходит с
    архивом ПОСЛЕ этого момента (Сборщик может параллельно дописывать
    новые заметки — A/B не должен от этого зависеть)."""
    all_cards = archive.load_cards_since(hours)
    notes_by_id = {c["id"]: c for c in all_cards}
    candidate_notes = [c for c in all_cards if c.get("importance", 0) >= config.V2_SELECTOR_MIN_IMPORTANCE]
    logger.info(
        "Заметок за %sч: %s всего, %s с importance >= %s",
        hours, len(all_cards), len(candidate_notes), config.V2_SELECTOR_MIN_IMPORTANCE,
    )
    last_titles = archive.load_last_issue_titles(config.V2_SELECTOR_LOOKBACK_ISSUES)

    selection = selector.select_stories(candidate_notes, last_titles)
    if selection is None:
        raise RuntimeError("Отборщик не вернул результат — A/B тест не может начаться")

    selected_ids = set(selection.get("selected_story_ids", []))
    stories = [s for s in selection.get("stories", []) if s["story_id"] in selected_ids]
    if not stories:
        raise RuntimeError("Отборщик не выбрал ни одного сюжета в этом окне — A/B тесту нечего сравнивать")

    used_note_ids = set()
    for s in stories:
        used_note_ids.update(s.get("note_ids", []))
    frozen_notes = {nid: notes_by_id[nid] for nid in used_note_ids if nid in notes_by_id}

    return {
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "hours_window": hours,
        "selection": selection,
        "stories": stories,
        "notes_by_id": frozen_notes,
        "last_titles": last_titles,
    }


def _run_one_participant(participant: dict, stories, notes_by_id, last_titles, usage_log: list) -> dict:
    """Один Аналитик (модель/effort/thinking_override — из participant) +
    один Фактчекер (всегда FACTCHECKER_MODEL) на замороженном входе."""
    label = participant["label"]

    def _on_analyst_usage(info):
        info = dict(info)
        info["stage"] = "analyst"
        info["participant"] = label
        info["cost_usd"] = _cost_usd(info["model"], info["input_tokens"], info["output_tokens"])
        usage_log.append(info)

    draft = analyst.write_issue(
        stories, notes_by_id,
        model=participant["model"],
        effort=participant["effort"],
        thinking_override=participant["thinking_override"],
        on_usage=_on_analyst_usage,
    )
    if draft is None:
        return {"label": label, "participant": participant, "draft": None, "error": "Аналитик не вернул результат"}

    used_note_ids = set(draft.get("watch_next_source_ids", []))
    for block in draft.get("blocks", []):
        used_note_ids.update(block.get("source_ids", []))
    factcheck_notes = {nid: notes_by_id[nid] for nid in used_note_ids if nid in notes_by_id}

    def _on_factcheck_usage(info):
        info = dict(info)
        info["stage"] = "factchecker"
        info["participant"] = label
        info["cost_usd"] = _cost_usd(info["model"], info["input_tokens"], info["output_tokens"])
        usage_log.append(info)

    factcheck = factchecker.check_issue(
        draft, factcheck_notes, last_titles,
        model=FACTCHECKER_MODEL,
        on_usage=_on_factcheck_usage,
    )
    if factcheck is None:
        return {
            "label": label, "participant": participant, "draft": draft,
            "factcheck": None, "error": "Фактчекер не вернул результат — черновик не проверен",
        }

    final_draft = factcheck.get("corrected_draft", draft)
    rendered = telegram_render.render_issue_html(final_draft)
    is_valid, html_errors = telegram_render.validate_telegram_html(rendered)

    return {
        "label": label,
        "participant": participant,
        "draft": draft,
        "factcheck": factcheck,
        "rendered": rendered,
        "html_valid": is_valid,
        "html_errors": html_errors,
        "error": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=int, default=12, help="окно заметок для Отборщика (по умолчанию 12ч)")
    parser.add_argument(
        "--force-reselect", action="store_true",
        help="вызвать Отборщик заново, даже если на сегодня уже есть замороженный файл",
    )
    parser.add_argument("--input", help="явный путь к уже замороженному файлу входа (пропускает Отборщик)")
    args = parser.parse_args()

    if not config.ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY не задан — без него ни одна из трёх моделей Аналитика не вызовется")
        return 1

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    frozen_path = args.input or os.path.join(OUTPUT_DIR, f"selector_frozen_{today}.json")

    if args.input:
        logger.info("=== Вход явно указан: %s (Отборщик не вызывается) ===", args.input)
        with open(args.input, "r", encoding="utf-8") as f:
            frozen = json.load(f)
    elif os.path.exists(frozen_path) and not args.force_reselect:
        logger.info(
            "=== Найден замороженный вход за сегодня (%s) — Отборщик НЕ вызывается повторно "
            "(--force-reselect, если это осознанно нужно) ===", frozen_path,
        )
        with open(frozen_path, "r", encoding="utf-8") as f:
            frozen = json.load(f)
    else:
        logger.info("=== Шаг 1: Отборщик (один раз за весь A/B-тест) ===")
        frozen = _freeze_selector_output(args.hours)
        with open(frozen_path, "w", encoding="utf-8") as f:
            json.dump(frozen, f, ensure_ascii=False, indent=2)
        logger.info("Вход заморожен в %s — все три Аналитика получат его как есть", frozen_path)

    stories = frozen["stories"]
    notes_by_id = frozen["notes_by_id"]
    last_titles = frozen["last_titles"]
    logger.info("Сюжетов на входе у всех трёх Аналитиков: %s", len(stories))

    usage_log: list[dict] = []
    results = []
    for participant in PARTICIPANTS:
        logger.info(
            "=== Аналитик: %s (effort=%s, thinking_override=%s) ===",
            participant["model"], participant["effort"], participant["thinking_override"],
        )
        result = _run_one_participant(participant, stories, notes_by_id, last_titles, usage_log)
        results.append(result)
        if result.get("error"):
            logger.error(
                "%s: %s — этот участник A/B пропущен в итоговом сравнении",
                participant["label"], result["error"],
            )

    usable = [r for r in results if r.get("rendered") is not None]
    if len(usable) < 2:
        logger.error("Меньше двух участников дошли до готового черновика — сравнивать нечего, см. ошибки выше")
        return 1

    # --- Вслепую: случайная буква на каждого участника -----------------------
    letters = ["A", "B", "C"][: len(usable)]
    random.shuffle(letters)
    for r, letter in zip(usable, letters):
        r["letter"] = letter
    usable.sort(key=lambda r: r["letter"])

    # --- Файл для автора: три черновика, без названий моделей ----------------
    for_author_path = os.path.join(OUTPUT_DIR, f"for_author_{today}.md")
    lines = [
        f"# A/B-тест Аналитика — черновики для чтения вслепую ({today})",
        "",
        "Критерии выбора (по порядку, решение автора — не автоматически): "
        "точность (см. фактчекер по каждому варианту ниже) → понятность и "
        "хук → реальность связей между сюжетами. При примерном равенстве по "
        "всем трём — решает стоимость (не в этом файле — см. технический "
        "файл ПОСЛЕ того, как выбор сделан).",
        "",
    ]
    for r in usable:
        fc = r["factcheck"]
        blocked, reason = factchecker.should_block(fc)
        lines.append(f"## Вариант {r['letter']}")
        lines.append("")
        lines.append(
            "**Фактчекер:** "
            f"unsupported_count={fc.get('unsupported_count', '?')}, "
            f"any_number_distorted={fc.get('any_number_distorted', '?')}, "
            f"hook_unsupported={fc.get('hook_unsupported', '?')}"
        )
        if blocked:
            lines.append(f"**Публикация была бы заблокирована фактчеком:** {reason}")
        if not r["html_valid"]:
            lines.append(f"**Внимание:** HTML-разметка невалидна для Telegram: {'; '.join(r['html_errors'])}")
        lines.append("")
        lines.append("```")
        lines.append(r["rendered"])
        lines.append("```")
        lines.append("")
    with open(for_author_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info("Файл для автора (вслепую, без названий моделей): %s", for_author_path)

    # --- Технический файл: буква<->модель + usage + стоимость ----------------
    technical_path = os.path.join(OUTPUT_DIR, f"technical_{today}.json")
    mapping = {
        r["letter"]: {
            "model": r["participant"]["model"],
            "effort": r["participant"]["effort"],
            "thinking_override": r["participant"]["thinking_override"],
        }
        for r in usable
    }
    total_cost = sum(u["cost_usd"] for u in usage_log if u.get("cost_usd") is not None)
    technical = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selector_input_file": frozen_path,
        "letter_to_model_mapping": mapping,
        "usage_by_call": usage_log,
        "total_cost_usd_estimate": round(total_cost, 4),
        "cost_caveat": (
            "Оценка по официальным ценам за 1M токенов (см. PRICING_PER_1M в "
            "этом скрипте) и реальным input_tokens/output_tokens из API. "
            "thinking-токены НЕ выделяются Anthropic API отдельным полем — "
            "они уже включены в output_tokens без разбивки, поэтому эта "
            "стоимость реальная (не только по видимому тексту ответа), но "
            "не отделяет explicitly, сколько из неё ушло на thinking."
        ),
        "errors": [
            {"label": r["label"], "model": r["participant"]["model"], "error": r["error"]}
            for r in results if r.get("error")
        ],
    }
    with open(technical_path, "w", encoding="utf-8") as f:
        json.dump(technical, f, ensure_ascii=False, indent=2)
    logger.info(
        "Технический файл (соответствие буква<->модель, usage, стоимость — "
        "автору НЕ показывать до выбора): %s", technical_path,
    )
    logger.info("Общая стоимость A/B-теста по факту (все успешные вызовы): $%.4f", total_cost)

    return 0


if __name__ == "__main__":
    sys.exit(main())
