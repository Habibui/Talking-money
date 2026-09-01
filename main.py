"""
Точка входа пайплайна. Запускается раз в час через GitHub Actions
(см. .github/workflows/publish.yml).

Логика:
1. Собрать свежие заголовки со всех источников.
2. Если это самый первый запуск (state пустой) — просто запомнить текущие
   заголовки как "уже виденные" и ничего не постить (иначе в канал сразу
   улетит вся история фидов).
3. Иначе — для каждого нового (ещё не опубликованного) заголовка получить
   перевод + комментарий от Claude и опубликовать в канал.

Переменная окружения DRY_RUN=1 — прогон без реальной отправки в Telegram
(текст постов печатается в лог), удобно для проверки перед боевым запуском.
"""

import logging
import os
import sys
import time

from src import config, llm, sources, state, telegram_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

SEND_DELAY_SECONDS = 3  # пауза между постами, чтобы не упереться в лимиты Telegram


def check_config() -> bool:
    missing = []
    if not config.TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not config.TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if not config.ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        logger.error("Не заданы переменные окружения: %s", ", ".join(missing))
        return False
    return True


def main() -> int:
    if not DRY_RUN and not check_config():
        return 1

    st = state.load_state()
    logger.info("Загружено состояние: %d известных id, bootstrapped=%s", len(st["ids"]), st["bootstrapped"])

    logger.info("Забираем заголовки из источников: %s", ", ".join(s["name"] for s in config.SOURCES))
    items = sources.fetch_all()
    logger.info("Всего получено %d заголовков из всех источников", len(items))

    if not st["bootstrapped"]:
        # первый запуск — не постим историю, просто запоминаем всё как виденное
        state.mark_posted(st, items)
        st["bootstrapped"] = True
        state.save_state(st)
        logger.info(
            "Первый запуск: запомнили %d заголовков без публикации. "
            "Со следующего запуска будут публиковаться только новые.",
            len(items),
        )
        return 0

    new_items = state.filter_new_items(st, items)
    logger.info("Новых (ещё не опубликованных) заголовков: %d", len(new_items))

    if not new_items:
        logger.info("Публиковать нечего.")
        return 0

    posted_count = 0
    for item in new_items:
        translated = llm.translate_and_comment(item)
        if translated is None:
            logger.warning("Пропускаем (не удалось перевести): %s — %s", item["source"], item["title"])
            continue

        text = telegram_bot.build_message(item, translated)

        if DRY_RUN:
            logger.info("[DRY_RUN] Пост из %s:\n%s\n", item["source"], text)
            ok = True
        else:
            ok = telegram_bot.send_message(text)

        if ok:
            state.mark_posted(st, [item])
            state.save_state(st)  # сохраняем сразу, чтобы при сбое не задвоить пост
            posted_count += 1
            if not DRY_RUN:
                time.sleep(SEND_DELAY_SECONDS)
        else:
            logger.warning("Пропускаем (не удалось отправить в Telegram): %s — %s", item["source"], item["title"])

    logger.info("Готово. Опубликовано постов: %d из %d новых.", posted_count, len(new_items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
