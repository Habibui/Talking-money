#!/usr/bin/env python3
"""
26.09.2026, вечер — продакшен-точка входа v2 для генерации выпуска
(минимальный запуск, решение менеджера — см. claude/format-v2-analytical-
brief.md в Cowork Project). Запускается два раза в день, 08:00 и 19:00 МСК
(.github/workflows/publish_v2.yml, mode=issue).

30.09.2026 — решение автора (п.1, гибридный формат v1+v2): теперь ОДИН
запуск в день, 22:00 МСК, а не два (см. cloudflare/worker.js). Окно заметок
для Отборщика (--hours ниже) соответственно расширено с 13ч до 25ч — было
"покрывает больший из двух промежутков между 08:00/19:00 с запасом", стало
"покрывает промежуток между соседними выпусками (24ч) с тем же примерно
запасом". v1 продолжает работать как раньше, это НЕ переход на v2-only.

26.09.2026, поздний вечер — п.2 правок менеджера: этот скрипт САМ первым
шагом запускает Сборщик (collector.run()), а не полагается на то, что
почасовой mode=collect уже отработал непосредственно перед ним. Раньше
план был — держать это гарантией через сдвиг cron в Cloudflare Worker на
5 минут (issue в :05, collect в :00), но у этого подхода есть встроенный
класс сбоя, который сам сдвиг не лечит: если конкретно ЭТОТ часовой
collect-запуск задержится (медленный источник, задержка выдачи раннера
GitHub Actions под нагрузкой — см. README.md, «Известные ограничения») —
выпуск в 08:05/19:05 всё равно уйдёт по чуть более старым данным, никакой
явной ошибки не будет. Самостоятельный collect() прямо здесь убирает эту
зависимость от чужого расписания целиком: выпуск гарантированно видит
максимально свежие заметки на момент своего собственного запуска, а не
«свежие настолько, насколько успел управиться соседний cron». Сдвиг в 5
минут в Cloudflare Worker всё равно оставлен (см. README.md) как
дополнительный, теперь не обязательный, запас на случай гонки самого
Worker'а — но корректность выпуска больше не зависит от него.

Сбой этого внутреннего collect() (сеть, временная проблема источника)
НЕ должен ронять весь выпуск — ловится и логируется отдельно (см. main()
ниже): лучше выпуск по чуть менее свежим уже накопленным заметкам, чем
никакой выпуск вовсе из-за сбоя, который следующий почасовой mode=collect
всё равно исправит сам.

Отличие от scripts/dry_run_v2.py: этот скрипт РЕАЛЬНО отправляет готовый
выпуск — не в канал (кнопок «Опубликовать»/«Отклонить» и обработчика через
Cloudflare Worker пока нет, это вторая неделя), а ЛИЧНЫМ сообщением автору
(TELEGRAM_AUTHOR_CHAT_ID). Автор публикует сам — пересылкой из личных
сообщений в канал со включённым «Скрыть имя отправителя» (см. Telegram:
удерживать сообщение → Переслать → переключатель имени отправителя).

Сценарий блокировки Фактчекера (should_block() = True): ВСЕГДА уходит
corrected_draft как основной черновик (не исходный, непроверенный) —
Фактчекер написан именно для того, чтобы автор публиковал уже исправленную
версию, а не оригинал с известными проблемами. Отдельным ВТОРЫМ сообщением
уходит разбор замечаний (factchecker.format_factcheck_report()) — только
когда есть что разбирать, то есть только при блокировке; при чистом
прогоне второго сообщения нет.

archive.append_issue() вызывается автоматически сразу после успешной
отправки автору (status="sent_to_author") — этого достаточно для того,
чтобы Отборщик следующего выпуска увидел заголовки через
archive.load_last_issue_titles() (см. src/selector.py, шаг 4 промпта).
Статус пока единственный — если появятся другие (например,
"published"/"rejected" на второй неделе, когда добавятся кнопки), они
допишутся отдельно, не меняя эту функцию.

Запуск:
  python3 scripts/issue_v2.py                  # окно по умолчанию (см. --hours)
  python3 scripts/issue_v2.py --hours=25        # явное окно заметок для Отборщика

ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_AUTHOR_CHAT_ID обязательны —
без последнего скрипт не может выполнить единственную свою задачу (доставить
выпуск автору), это явная, а не тихая ошибка (см. main() ниже)."""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import archive, collector, config, factchecker, analyst, selector, telegram_bot, telegram_render, timeutil

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("issue_v2")


def _send_to_author(text: str, parse_mode: str | None = "HTML") -> bool:
    # 01.10.2026, вечер — баг, найденный автором: выпуск в личку приходил со
    # звуком даже ночью (23:00-08:00 МСК) — send_message() здесь никогда не
    # получал silent=, поэтому действовал дефолт False. v1 (main.py) уже
    # правильно использует timeutil.is_night_msk() для своих отправок, этот
    # фикс просто доводит ту же проверку до v2. Смотрим на момент самой
    # отправки (а не планирования) — так же, как и у v1.
    ok = telegram_bot.send_message(
        text, chat_id=config.TELEGRAM_AUTHOR_CHAT_ID, disable_preview=True, parse_mode=parse_mode,
        silent=timeutil.is_night_msk(),
    )
    if not ok:
        logger.error("Не удалось отправить сообщение автору (chat_id=%s)", config.TELEGRAM_AUTHOR_CHAT_ID)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--hours", type=int, default=25,
        help="окно заметок для Отборщика в часах (по умолчанию 25ч — при одном выпуске в сутки, "
             "22:00 МСК, промежуток между соседними выпусками 24ч, плюс небольшой запас; "
             "повторный захват уже отобранного сюжета Отборщик отсекает сам через сюжеты прошлых "
             "выпусков, шаг 4 его промпта, так что запас по времени не создаёт риска задвоения)",
    )
    args = parser.parse_args()

    missing = [
        name for name, val in (
            ("ANTHROPIC_API_KEY", config.ANTHROPIC_API_KEY),
            ("TELEGRAM_BOT_TOKEN", config.TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_AUTHOR_CHAT_ID", config.TELEGRAM_AUTHOR_CHAT_ID),
        ) if not val
    ]
    if missing:
        logger.error("Не заданы обязательные переменные окружения: %s", ", ".join(missing))
        return 1

    logger.info("=== Шаг 0: Сборщик (свежие заметки перед формированием выпуска) ===")
    try:
        collect_stats = collector.run()
        logger.info("Сборщик перед выпуском: %s", collect_stats)
    except Exception:
        # Намеренно не падаем из-за этого — см. docstring модуля выше:
        # выпуск по чуть менее свежим уже накопленным заметкам лучше, чем
        # никакой выпуск из-за временного сбоя одного источника/сети.
        logger.exception(
            "Сборщик перед выпуском упал с исключением — продолжаем на "
            "уже накопленных заметках (следующий часовой mode=collect всё "
            "равно подхватит то, что не удалось сейчас)"
        )

    logger.info("=== Шаг 1: Отборщик ===")
    all_cards = archive.load_cards_since(args.hours)
    notes_by_id = {c["id"]: c for c in all_cards}
    important_notes = [c for c in all_cards if c.get("importance", 0) >= config.V2_SELECTOR_MIN_IMPORTANCE]
    # 03.10.2026 — свежесть: в выпуск 03.10 попала новость 29.09 (Сборщик
    # добрал её в архив только 02.10), а окно по extracted_at этого не видит.
    candidate_notes, stale_notes = archive.filter_fresh(important_notes, config.V2_MAX_NOTE_AGE_HOURS)
    logger.info(
        "Заметок за %sч: %s всего, %s с importance >= %s, из них устарели (старше %sч) %s, к Отборщику идут %s",
        args.hours, len(all_cards), len(important_notes), config.V2_SELECTOR_MIN_IMPORTANCE,
        config.V2_MAX_NOTE_AGE_HOURS, len(stale_notes), len(candidate_notes),
    )
    for c in stale_notes:
        logger.info("  устарела: %s %s (%.0fч)", c.get("source"), c.get("link"), archive.note_age_hours(c))

    last_titles = archive.load_last_issue_titles(config.V2_SELECTOR_LOOKBACK_ISSUES)
    last_context = archive.load_last_issue_summaries(config.V2_SELECTOR_LOOKBACK_ISSUES)
    selection = selector.select_stories(candidate_notes, last_titles, last_issue_context=last_context)
    if selection is None:
        logger.error("Отборщик не вернул результат — выпуск не сформирован в этом окне")
        return 1

    # 03.10.2026 — программные фильтры поверх выбора Отборщика (вектор канала,
    # потолок в 4 сюжета) и порог «достаточно сюжетов, чтобы вообще выходить».
    stories, dropped = selector.apply_gates(selection)
    for d in dropped:
        logger.info("Отсечено фильтром вектора канала: %s", d)
    if len(stories) < config.V2_MIN_STORIES_FOR_ISSUE:
        logger.info(
            "Сюжетов после фильтров %s < минимума %s — выпуска не будет",
            len(stories), config.V2_MIN_STORIES_FOR_ISSUE,
        )
        # Автору — короткое сообщение, а не тишина: иначе «выпуска нет, потому
        # что нечего публиковать» неотличимо от «пайплайн сломался».
        reason_lines = [
            f"Выпуск за сегодня не сформирован: подходящих сюжетов {len(stories)}, "
            f"нужно минимум {config.V2_MIN_STORIES_FOR_ISSUE}.",
            f"Новостей, отсеянных как устаревшие (старше {config.V2_MAX_NOTE_AGE_HOURS}ч): {len(stale_notes)}.",
        ]
        if dropped:
            reason_lines.append("Отсечено как не по вектору канала: " + "; ".join(dropped) + ".")
        _send_to_author("\n".join(reason_lines), parse_mode=None)
        return 0

    logger.info("=== Шаг 2: Аналитик ===")
    draft = analyst.write_issue(stories, notes_by_id)
    if draft is None:
        logger.error("Аналитик не вернул результат — выпуск не сформирован")
        return 1

    logger.info("=== Шаг 3: Фактчекер ===")
    used_note_ids = set(draft.get("watch_next_source_ids", []))
    for block in draft.get("blocks", []):
        used_note_ids.update(block.get("source_ids", []))
    factcheck_notes = {nid: notes_by_id[nid] for nid in used_note_ids if nid in notes_by_id}

    factcheck = factchecker.check_issue(draft, factcheck_notes, last_titles)
    if factcheck is None:
        logger.error(
            "Фактчекер не вернул результат — выпуск НЕ отправляется автору "
            "(сбой самой проверки — не то же самое, что «проверка прошла успешно»)"
        )
        return 1

    blocked, reason = factchecker.should_block(factcheck)
    final_draft = factcheck.get("corrected_draft", draft)

    logger.info("=== Шаг 4: рендер + отправка автору ===")
    rendered = telegram_render.render_issue_html(final_draft)
    is_valid, html_errors = telegram_render.validate_telegram_html(rendered)

    if is_valid:
        send_text, parse_mode = rendered, "HTML"
    else:
        logger.warning(
            "HTML-разметка невалидна для Telegram (%s) — уходит текст без разметки: %s",
            len(html_errors), "; ".join(html_errors),
        )
        send_text, parse_mode = telegram_render.strip_telegram_html(rendered), None

    prefix = "⚠️ Фактчекер заблокировал бы публикацию — черновик ниже уже ИСПРАВЛЕН:\n\n" if blocked else ""
    if not _send_to_author(prefix + send_text, parse_mode=parse_mode):
        return 1
    logger.info("Выпуск отправлен автору в личку (chat_id=%s)", config.TELEGRAM_AUTHOR_CHAT_ID)

    if blocked:
        logger.warning("Фактчек заблокировал бы публикацию: %s — отправляем разбор вторым сообщением", reason)
        report = factchecker.format_factcheck_report(factcheck)
        _send_to_author(report, parse_mode=None)

    record = {
        "issue_id": f"v2-{datetime.now(timezone.utc).isoformat()}",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "story_titles": [s["title"] for s in stories],
        "draft": final_draft,
        # 26.09.2026 — status="sent_to_author": единственный статус
        # минимального запуска (см. docstring модуля выше) — публикация в
        # канал делается автором вручную, скрипт этого не видит и не может
        # отметить отдельным статусом до появления кнопок/вебхука.
        "status": "sent_to_author",
        "factcheck_blocked": blocked,
    }
    archive.append_issue(record)
    logger.info("Выпуск записан в archive/issues.jsonl (status=sent_to_author)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
