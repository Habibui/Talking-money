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

from src import config, dedup, llm, sources, state, telegram_bot, timeutil

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

    silent = timeutil.is_night_msk()
    sent_all = True
    for msg in digest_messages:
        if not telegram_bot.send_message(msg, silent=silent):
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
    skipped_near_duplicates = 0
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

        # Модель уже сверяла эту новость со списком recent_posts и сама решила,
        # что это не дубль (иначе story_status был бы "duplicate" выше). Но
        # 16.09.2026 обнаружилось, что модель систематически пропускает именно
        # дубли между СВЕЖИМИ заметками об одном и том же событии — типично от
        # одного источника-агрегатора с разницей в секунды-минуты (решение +
        # реакция рынка + прогноз, всё про одно и то же) — и каждая уходила с
        # story_status="new", раздувая дайджест копиями (см.
        # claude/pipeline-v1-setup.md, инцидент 16.09.2026). Поэтому —
        # независимая от LLM подстраховка на простом текстовом сходстве (см.
        # src/dedup.py), применяется КО ВСЕМ новостям, а не только к
        # "breaking", как было раньше (раньше здесь было только мягкое
        # понижение breaking → routine — этого оказалось недостаточно, т.к.
        # подавляющее большинство обнаруженных 16.09 дублей изначально шли
        # как "routine", и та версия фильтра их вообще не проверяла). При
        # срабатывании обрабатываем точно так же, как LLM-дубль выше — не
        # публикуем и не копим в дайджест.
        is_near_dup, dup_score, dup_match = dedup.is_near_duplicate(
            translated["headline_ru"], translated["comment_ru"], recent_posts
        )

        # 18.09.2026: "определённые" дубли (условия 1/2 в dedup.py) решаются
        # формулой прямо выше. Но есть отдельный пограничный случай (условие
        # 3 — 2+ общих значимых токена при слабом остальном сходстве текста),
        # для которого доказано (см. claude/pipeline-v1-setup.md, инцидент
        # 18.09.2026), что формула/порог принципиально не могут отличить
        # настоящий дубль (например, три источника про один и тот же хайк
        # ставки Банка Японии) от случайного совпадения фоновых слов
        # (например, "ФРС"+"нефть $100" у двух не связанных новостей). Такие
        # случаи не решаем формулой — отдаём на решение той же модели, что и
        # переводит новости, но отдельным узким вопросом (см.
        # llm.confirm_same_event) вместо ещё одного порога.
        if not is_near_dup:
            ambiguous = dedup.find_ambiguous_match(
                translated["headline_ru"], translated["comment_ru"], recent_posts
            )
            if ambiguous is not None:
                amb_jaccard, amb_salient, amb_match = ambiguous
                new_text = f"{translated['headline_ru']} {translated['comment_ru']}"
                old_text = f"{amb_match.get('headline_ru', '')} {amb_match.get('comment_ru', '')}"
                same_event = llm.confirm_same_event(new_text, old_text)
                logger.info(
                    "Пограничный случай дедупа (jaccard=%.2f, значимых токенов=%d) "
                    "с [%s] %r — точечная проверка модели вернула %r: %s — %s",
                    amb_jaccard, amb_salient, amb_match["source"], amb_match["headline_ru"],
                    same_event, item["source"], item["title"],
                )
                # 19.09.2026: тот же случай — отдельной записью в постоянный
                # журнал (state/dedup_escalations.json), а не только в лог
                # запуска — логи GitHub Actions хранятся ограниченное время и
                # искать по многим запускам вручную неудобно; журнал
                # переживает запуски и разбирается одним скриптом (см.
                # scripts/review_escalations.py). Не влияет на публикацию —
                # чисто накопление данных для будущей оценки точности
                # confirm_same_event на практике.
                state.append_dedup_escalation(
                    jaccard=amb_jaccard,
                    salient_overlap=amb_salient,
                    same_event=same_event,
                    new_source=item["source"],
                    new_headline_ru=translated["headline_ru"],
                    new_comment_ru=translated["comment_ru"],
                    old_source=amb_match["source"],
                    old_headline_ru=amb_match["headline_ru"],
                    old_comment_ru=amb_match.get("comment_ru", ""),
                )
                if same_event:
                    is_near_dup, dup_score, dup_match = True, amb_jaccard, amb_match
                # same_event is False или None (сбой проверки) — публикуем как
                # обычно; см. docstring confirm_same_event, почему сбой не
                # должен блокировать публикацию.

        if is_near_dup:
            logger.warning(
                "Пропускаем (текстовое сходство %.2f с уже опубликованным "
                "[%s] %r, story_status от модели был %r): %s — %s",
                dup_score, dup_match["source"], dup_match["headline_ru"],
                story_status, item["source"], item["title"],
            )
            state.mark_posted(st, [item])
            state.save_state(st)
            skipped_near_duplicates += 1
            continue

        # 23.09.2026: страховка после перевода — усиление SYSTEM_PROMPT
        # (коммит 6746f7d, 22.09.2026) само по себе не удержало модель от
        # того, чтобы иногда оставлять известные сокращения как есть (BofA,
        # OECD и т.п.), причём проблема повторилась уже на следующий день
        # после деплоя фикса, не из старой очереди (см.
        # claude/pipeline-v1-setup.md, инцидент 22-23.09.2026). Проверяем
        # ТОЛЬКО тексты, которые реально дойдут до публикации (после проверок
        # на дубли выше) — основной объём текстов вообще не содержит
        # известных сокращений и лишнего вызова не получает.
        found_abbrevs = llm.find_known_abbreviations(
            translated["headline_ru"], translated["comment_ru"]
        )
        if found_abbrevs:
            fixed = llm.fix_abbreviations(
                translated["headline_ru"], translated["comment_ru"], found_abbrevs
            )
            if fixed:
                translated["headline_ru"], translated["comment_ru"] = fixed
                logger.info(
                    "Заменены сокращения (%s): %s — %s",
                    ", ".join(found_abbrevs), item["source"], item["title"],
                )
            else:
                # сбой точечной правки — публикуем оригинал с сокращением как
                # есть, не блокируем публикацию из-за необязательного шага
                logger.warning(
                    "Не удалось точечно поправить сокращения (%s), публикуем "
                    "как есть: %s — %s",
                    ", ".join(found_abbrevs), item["source"], item["title"],
                )

        urgency = translated.get("urgency", "routine")

        if urgency == "breaking":
            text = telegram_bot.build_message(item, translated)

            if DRY_RUN:
                logger.info("[DRY_RUN] Пост из %s:\n%s\n", item["source"], text)
                ok = True
            else:
                ok = telegram_bot.send_message(text, silent=timeutil.is_night_msk())

            if ok:
                state.mark_posted(st, [item])
                state.save_state(st)  # сохраняем сразу, чтобы при сбое не задвоить пост
                recent_posts = state.append_recent_post(
                    recent_posts, item["source"], translated["headline_ru"], translated["comment_ru"]
                )
                posted_count += 1
                # story_status пишем в лог явно (не только для "duplicate", как
                # раньше) — иначе при подозрении на дубль между источниками
                # (модель сочла его не дублем, а "new"/"update") нет способа
                # проверить постфактум, что именно вернула модель и было ли у
                # неё вообще на входе достаточно recent_posts для сравнения.
                logger.info(
                    "Опубликовано (breaking, story_status=%s, recent_posts=%d): %s — %s",
                    story_status, len(recent_posts) - 1, item["source"], item["title"],
                )
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
        logger.info(
            "В очередь дайджеста (story_status=%s, recent_posts=%d): %s — %s",
            story_status, len(recent_posts) - 1, item["source"], item["title"],
        )

    logger.info(
        "Готово. Обработано постов: %d из %d новых (пропущено дублей по мнению "
        "модели: %d, пропущено по текстовому сходству: %d).",
        posted_count, len(new_items), skipped_duplicates, skipped_near_duplicates,
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
