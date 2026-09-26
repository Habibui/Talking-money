#!/usr/bin/env python3
"""
26.09.2026. Прогоняет весь пайплайн v2 (Сборщик -> Отборщик -> Аналитик ->
Фактчекер -> рендер) от начала до конца ОДНИМ запуском, НИЧЕГО не публикуя
в Telegram — печатает результат каждого шага в консоль. Это первая
рабочая версия кода v2 (см. claude/format-v2-prompts-draft.md, третья
версия, и claude/format-v2-analytical-brief.md, план "Сб: сборщик, пул,
отбор, анализ, фактчек, всё в DRY_RUN") — предназначена для запуска на
машине с реальным ANTHROPIC_API_KEY (в облачной песочнице, где этот код
писался, такого ключа нет — см. README.md/pipeline-v1-setup.md про
конвенцию проверки офлайн через мок клиента + отдельный реальный прогон
человеком).

НЕ публикует в Telegram (нет ни одного вызова telegram_bot) и по умолчанию
НЕ дописывает выпуск в archive/issues.jsonl (см. --commit-issue) — обычный
DRY_RUN не должен загрязнять историю выпусков, которую Отборщик использует
для проверки повтора сюжета (шаг 4 его промпта); используйте --commit-issue
только для осознанного "этот прогон — настоящий, засчитать его в историю".

Запуск:
  python3 scripts/dry_run_v2.py                  # сборщик + весь пайплайн
  python3 scripts/dry_run_v2.py --skip-collect    # без сборщика, на уже
                                                   # накопленном архиве заметок
  python3 scripts/dry_run_v2.py --hours=48        # окно заметок для Отборщика
  python3 scripts/dry_run_v2.py --commit-issue    # засчитать выпуск в историю

Известные ограничения этой первой версии (см. src/analyst.py, docstring
модуля): «сжатая история выпусков за 14 дней» и «релевантные заметки по
тегам из архива» Аналитику пока не передаются (retrieval для них — не
реализован) — на первых прогонах это ожидаемо, не баг этого скрипта.
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import archive, collector, config, factchecker, analyst, selector, telegram_render

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("dry_run_v2")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-collect", action="store_true", help="не запускать Сборщик, взять уже накопленный архив")
    parser.add_argument("--hours", type=int, default=12, help="окно заметок для Отборщика (по умолчанию 12ч)")
    parser.add_argument("--commit-issue", action="store_true", help="дописать результат в archive/issues.jsonl")
    args = parser.parse_args()

    if not config.ANTHROPIC_API_KEY:
        logger.error(
            "ANTHROPIC_API_KEY не задан в окружении — без него ни один шаг "
            "(Экстрактор/Отборщик/Аналитик/Фактчекер) не сможет вызвать "
            "Claude API. Задайте переменную окружения перед запуском."
        )
        return 1

    if not args.skip_collect:
        logger.info("=== Шаг 1: Сборщик ===")
        stats = collector.run()
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        logger.info("=== Шаг 1: Сборщик — пропущен (--skip-collect) ===")

    logger.info("=== Шаг 2: Отборщик ===")
    all_cards = archive.load_cards_since(args.hours)
    notes_by_id = {c["id"]: c for c in all_cards}
    candidate_notes = [c for c in all_cards if c.get("importance", 0) >= config.V2_SELECTOR_MIN_IMPORTANCE]
    logger.info(
        "Заметок за %sч: %s всего, %s с importance >= %s",
        args.hours, len(all_cards), len(candidate_notes), config.V2_SELECTOR_MIN_IMPORTANCE,
    )

    last_titles = archive.load_last_issue_titles(config.V2_SELECTOR_LOOKBACK_ISSUES)
    selection = selector.select_stories(candidate_notes, last_titles)
    if selection is None:
        logger.error("Отборщик не вернул результат — прогон остановлен")
        return 1
    print(json.dumps(selection, ensure_ascii=False, indent=2))

    selected_ids = set(selection.get("selected_story_ids", []))
    stories = [s for s in selection.get("stories", []) if s["story_id"] in selected_ids]
    if not stories:
        logger.info("Отборщик не выбрал ни одного сюжета для выпуска в этом окне — прогон завершён без выпуска")
        return 0

    logger.info("=== Шаг 3: Аналитик ===")
    draft = analyst.write_issue(stories, notes_by_id)
    if draft is None:
        logger.error("Аналитик не вернул результат — прогон остановлен")
        return 1
    print(json.dumps(draft, ensure_ascii=False, indent=2))

    logger.info("=== Шаг 4: Фактчекер ===")
    used_note_ids = set(draft.get("watch_next_source_ids", []))
    for block in draft.get("blocks", []):
        used_note_ids.update(block.get("source_ids", []))
    factcheck_notes = {nid: notes_by_id[nid] for nid in used_note_ids if nid in notes_by_id}

    factcheck = factchecker.check_issue(draft, factcheck_notes, last_titles)
    if factcheck is None:
        logger.error(
            "Фактчекер не вернул результат — выпуск НЕ считается проверенным, "
            "публикация (в реальном режиме) должна быть заблокирована"
        )
        return 1
    print(json.dumps(factcheck, ensure_ascii=False, indent=2))

    blocked, reason = factchecker.should_block(factcheck)
    if blocked:
        logger.warning("Публикация была бы ЗАБЛОКИРОВАНА фактчеком: %s", reason)
    else:
        logger.info("Фактчек пройден без блокирующих замечаний")

    final_draft = factcheck.get("corrected_draft", draft)
    rendered = telegram_render.render_issue_html(final_draft)
    is_valid, html_errors = telegram_render.validate_telegram_html(rendered)

    print("\n=== Итоговый текст выпуска (HTML для Telegram) ===\n")
    print(rendered)
    print(f"\n=== Длина: {len(rendered)} символов ===")
    if not is_valid:
        logger.warning(
            "HTML-разметка НЕВАЛИДНА для Telegram (%s) — в реальной отправке "
            "ушёл бы plain-text вариант + уведомление автору, см. "
            "src/telegram_render.py: %s", len(html_errors), "; ".join(html_errors),
        )
    else:
        logger.info("HTML-разметка валидна для Telegram")

    if args.commit_issue:
        import datetime as _dt
        record = {
            "issue_id": f"dryrun-{_dt.datetime.now(_dt.timezone.utc).isoformat()}",
            "published_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "story_titles": [s["title"] for s in stories],
            "draft": final_draft,
        }
        archive.append_issue(record)
        logger.info("Выпуск дописан в archive/issues.jsonl (--commit-issue)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
