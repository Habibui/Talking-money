"""
Точка входа пайплайна. Запускается каждые ~15 минут через внешний
Cloudflare Worker (workflow_dispatch), с резервным редким schedule: в самом
GitHub Actions (см. .github/workflows/publish.yml).

Логика:
1. Собрать свежие заголовки со всех источников.
2. Если это самый первый запуск (state пустой) — просто запомнить текущие
   заголовки как "уже виденные" и ничего не постить (иначе в канал сразу
   улетит вся история фидов).
3. Если сейчас "час флаша" (см. config.DIGEST_FLUSH_HOURS_MSK) и с прошлого
   раза накопилась очередь рутинных новостей — сначала отправить её одним
   дайджестом.
4. Для каждого нового (ещё не опубликованного) заголовка получить перевод +
   комментарий от Claude (модель заодно решает: подходит ли новость каналу,
   это дубль/апдейт уже известной истории или нет, и насколько она "громкая").
   Громкое публикуется сразу; рутина копится в очередь до ближайшего дайджеста.

Переменная окружения DRY_RUN=1 — прогон без реальной отправки в Telegram
(текст постов печатается в лог), удобно для проверки перед боевым запуском.
"""

import logging
import os
import sys
import time

from src import config, llm, sources, state, telegram_bot, timeutil

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


def maybe_flush_digest() -> None:
    """Если сейчас час из config.DIGEST_FLUSH_HOURS_MSK и в этот час ещё не
    флашили — отправляет накопленную очередь рутинных новостей одним
    дайджестом (если она не пуста), и в любом случае отмечает этот час как
    обработанный, чтобы не пытаться флашить повторно на каждом из нескольких
    запусков внутри одного и того же часа."""
    current_hour = timeutil.hour_msk()
    flush_key = timeutil.flush_key_msk()
    is_flush_hour = current_hour in config.DIGEST_FLUSH_HOURS_MSK

    meta = state.load_digest_meta()
    if not is_flush_hour or meta.get("last_flush_key") == flush_key:
        return

    digest_queue = state.load_digest_queue()
    if not digest_queue:
        # нечего слать, но час всё равно отмечаем — иначе следующий запуск
        # в этом же часе (через 15 минут) будет проверять это же условие снова
        meta["last_flush_key"] = flush_key
        state.save_digest_meta(meta)
        return

    intro_ru = llm.summarize_digest(digest_queue)
    digest_messages = telegram_bot.build_digest_messages(digest_queue, intro_ru)

    if DRY_RUN:
        for i, msg in enumerate(digest_messages, 1):
            logger.info("[DRY_RUN] Дайджест %d/%d:\n%s\n", i, len(digest_messages), msg)
        state.save_digest_queue([])
        meta["last_flush_key"] = flush_key
        state.save_digest_meta(meta)
        logger.info("[DRY_RUN] Дайджест из %d новостей 'отправлен'.", len(digest_queue))
        return

    sent_all = True
    for msg in digest_messages:
        if not telegram_bot.send_message(msg):
            sent_all = False
            logger.warning(
                "Не удалось отправить часть дайджеста — очередь и час флаша "
                "оставляем как есть, попробуем снова следующим запуском."
            )
            break
        time.sleep(SEND_DELAY_SECONDS)

    if sent_all:
        state.save_digest_queue([])
        meta["last_flush_key"] = flush_key
        state.save_digest_meta(meta)
        logger.info(
            "Дайджест отправлен: %d новостей, %d сообщени(е/я/й).",
            len(digest_queue), len(digest_messages),
        )


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

    maybe_flush_digest()

    new_items = state.filter_new_items(st, items)
    logger.info("Новых (ещё не опубликованных) заголовков: %d", len(new_items))

    if not new_items:
        logger.info("Публиковать нечего.")
        return 0

    recent_posts = state.load_recent_posts()

    posted_count = 0
    skipped_duplicates = 0
    translate_failures = 0
    for item in new_items:
        # пробуем прочитать саму статью (полный текст через trafilatura,
        # либо og:description как запасной вариант) — это даёт модели больше
        # материала для собственной выжимки, чем голый RSS/телеграм-summary;
        # если не получилось — работаем с тем summary, что уже есть, пайплайн
        # из-за этого не должен падать
        richer_lead = sources.fetch_article_lead(item["link"])
        if richer_lead:
            item["summary"] = richer_lead

        # логируем, что именно уходит модели на вход — иначе при подозрении
        # на выдумку факта нет способа проверить, что реально было в основе
        # перевода/комментария; урезаем до 500 символов, чтобы не раздувать
        # лог, но с указанием полной длины текста
        preview = item["summary"][:500]
        if len(item["summary"]) > 500:
            preview += "…"
        logger.info(
            "Вход для %s (%d символов): %s",
            item["source"],
            len(item["summary"]),
            preview,
        )

        translated = llm.translate_and_comment(item, recent_posts=recent_posts)
        if translated is None:
            logger.warning("Пропускаем (не удалось перевести): %s — %s", item["source"], item["title"])
            translate_failures += 1
            continue

        if not translated.get("relevant", True):
            # не подходит по теме (лайфстайл/спорт-как-бизнес/политика без
            # экономической связки и т.п.) — считаем обработанным, чтобы не
            # пытаться снова, но в канал не публикуем
            logger.info("Пропускаем (не по теме канала): %s — %s", item["source"], item["title"])
            state.mark_posted(st, [item])
            state.save_state(st)
            continue

        story_status = translated.get("story_status", "new")
        if story_status == "duplicate":
            # та же история, что уже публиковали (с другого источника), и без
            # ничего нового по сути — не публикуем, но помечаем обработанным
            logger.info("Пропускаем (дубль уже опубликованной истории): %s — %s", item["source"], item["title"])
            state.mark_posted(st, [item])
            state.save_state(st)
            skipped_duplicates += 1
            continue

        urgency = translated.get("urgency", "routine")

        if urgency == "breaking":
            text = telegram_bot.build_message(item, translated)

            if DRY_RUN:
                logger.info("[DRY_RUN] Пост из %s:\n%s\n", item["source"], text)
                ok = True
            else:
                ok = telegram_bot.send_message(text)

            if ok:
                state.mark_posted(st, [item])
                state.save_state(st)  # сохраняем сразу, чтобы при сбое не задвоить пост
                recent_posts = state.append_recent_post(
                    recent_posts, item["source"], translated["headline_ru"], translated["comment_ru"]
                )
                posted_count += 1
                if not DRY_RUN:
                    time.sleep(SEND_DELAY_SECONDS)
            else:
                logger.warning("Пропускаем (не удалось отправить в Telegram): %s — %s", item["source"], item["title"])
            continue

        # routine — не публикуем сразу, копим в очередь дайджеста. Порядок —
        # сначала очередь и recent_posts, потом отметка "опубликовано" в
        # основном state: если пайплайн упадёт между этими шагами, лучше
        # повторно обработать заголовок (дубль в очереди не страшнее дубля
        # поста), чем молча потерять уже переведённую новость.
        state.append_to_digest_queue(
            item["source"], translated["headline_ru"], translated["comment_ru"], item["link"]
        )
        recent_posts = state.append_recent_post(
            recent_posts, item["source"], translated["headline_ru"], translated["comment_ru"]
        )
        state.mark_posted(st, [item])
        state.save_state(st)
        posted_count += 1
        logger.info("В очередь дайджеста: %s — %s", item["source"], item["title"])

    logger.info(
        "Готово. Обработано постов: %d из %d новых (пропущено дублей: %d).",
        posted_count, len(new_items), skipped_duplicates,
    )

    if translate_failures > 0 and translate_failures == len(new_items):
        # ни одна новость не перевелась — это не "модели не повезло на одном
        # заголовке", а похоже на системный сбой (например, не оплачен/истёк
        # ключ Anthropic). Сам перевод ошибку проглатывает и возвращает None,
        # поэтому явно проваливаем запуск, чтобы сработало уведомление о
        # сбое (см. .github/workflows/publish.yml, шаг "Уведомить о сбое") —
        # иначе публикации молча прекратятся, а мы об этом не узнаем.
        logger.error(
            "Все %d новых заголовков не удалось перевести — похоже на системный сбой "
            "(например, не оплачен/истёк ключ Anthropic), а не на единичную ошибку.",
            translate_failures,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
