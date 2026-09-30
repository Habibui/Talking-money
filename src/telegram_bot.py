"""
Публикация сообщения в канал через Telegram Bot API.
"""

import html
import logging
import re

import requests

from . import config

logger = logging.getLogger(__name__)

# Лимит Telegram на длину text у sendMessage — 4096 символов (считая HTML-разметку
# в самой строке). Дайджест из нескольких новостей может в него не влезть —
# тогда режем на несколько сообщений подряд.
TELEGRAM_TEXT_LIMIT = 4096


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


# 01.10.2026 — решение автора: модель (src/llm.py, SYSTEM_PROMPT, правило 4)
# выделяет главную мысль comment_ru текстовым маркером **вот так**, а не
# HTML-тегом напрямую — безопаснее (модель никогда не пишет сырой HTML,
# который нужно было бы доверять как есть), и один и тот же маркер работает
# одинаково что в одиночном посте, что в строке дайджеста. Применять ПОСЛЕ
# _esc() — звёздочки не экранируются html.escape(), так что порядок
# "сначала экранировать, потом заменить маркер на <b>" ничего не ломает.
# Пары ищем нежадно (.+?) — промпт просит не больше одной пары на
# comment_ru, но если модель всё же поставит несколько, все валидные пары
# просто станут жирными, а не одна случайная половина разметки.
_BOLD_MARKER_RE = re.compile(r"\*\*(.+?)\*\*")


def _apply_bold_marker(escaped_text: str) -> str:
    return _BOLD_MARKER_RE.sub(r"<b>\1</b>", escaped_text)


def _news_block(source: str, headline_ru: str, comment_ru: str, link: str) -> str:
    # 01.10.2026 — решение автора: убрали заголовок как отдельный жирный
    # блок (было <b>headline</b>\n\n comment) — теперь headline_ru это
    # цепляющая ПЕРВАЯ ФРАЗА поста (см. llm.py, правило 3), а не заголовок
    # над текстом, поэтому headline и comment идут одним связным абзацем, не
    # жирным и не отделённым пустой строкой. Жирным выделяется не headline,
    # а конкретная мысль ВНУТРИ comment_ru — через **маркер**, см.
    # _apply_bold_marker выше.
    headline = _esc(headline_ru)
    comment = _apply_bold_marker(_esc(comment_ru))
    source_esc = _esc(source)
    return (
        f"{headline} {comment}\n\n"
        f'<a href="{link}">{source_esc} →</a>'
    )


def build_message(item: dict, translated: dict) -> str:
    return _news_block(item["source"], translated["headline_ru"], translated["comment_ru"], item["link"])


# 30.09.2026 — фиксированный набор тем дайджеста (решение автора, п.3) —
# код держит соответствие тег→эмодзи, а не модель: модель (src/llm.py,
# SYSTEM_PROMPT, правило 11) выбирает только текстовый код темы
# (topic_tag), никогда сам символ эмодзи — так одна опечатка модели не
# протащит в канал случайный/неразрешённый эмодзи. Ключи должны совпадать
# с llm.VALID_TOPIC_TAGS.
_DIGEST_TOPIC_EMOJI = {
    "oil_gas": "🛢",
    "energy": "⚡",
    "markets": "📈",
    "central_banks": "🏦",
    "geopolitics": "🌍",
    "russia": "🇷🇺",
}
_DIGEST_DEFAULT_EMOJI = _DIGEST_TOPIC_EMOJI["markets"]


def _digest_line(source: str, comment_ru: str, link: str, topic_tag: str | None) -> str:
    """Формат одной новости внутри дайджеста (решение автора 30.09.2026,
    п.3) — БЕЗ заголовка статьи (headline_ru здесь сознательно не
    используется): эмодзи-тег темы + одна строка сути + источник как
    слово-ссылка. "Строка сути" — это comment_ru (собственный комментарий
    канала, уже короткий по своей природе из-за SYSTEM_PROMPT в llm.py) —
    отдельного более короткого текста для дайджеста модель не пишет, чтобы
    не плодить ещё один LLM-вызов только ради формата. topic_tag — код
    темы из llm.VALID_TOPIC_TAGS; None или незнакомое значение (например,
    у новости, поставленной в очередь ДО этого деплоя — в старых записях
    state/digest_queue.json этого поля просто не было) — безопасный
    дефолт _DIGEST_DEFAULT_EMOJI, а не ошибка. 01.10.2026 — comment_ru может
    содержать **маркер** главной мысли (см. llm.py, правило 4) — та же
    _apply_bold_marker(), что и в одиночном посте, иначе в дайджесте
    остались бы видны сырые звёздочки."""
    emoji = _DIGEST_TOPIC_EMOJI.get(topic_tag, _DIGEST_DEFAULT_EMOJI)
    comment = _apply_bold_marker(_esc(comment_ru))
    source_esc = _esc(source)
    return f'{emoji} {comment} — <a href="{link}">{source_esc}</a>'


def build_digest_messages(queue_items: list, intro_ru: str | None = None) -> list:
    """Собирает накопленные рутинные новости в один пост (или несколько, если
    не влезает в лимит Telegram). queue_items — список dict с ключами
    source/headline_ru/comment_ru/link/topic_tag (см.
    state.append_to_digest_queue; topic_tag может отсутствовать у записей
    из очереди до 30.09.2026 — см. _digest_line). intro_ru — фраза-интро в
    фирменном тоне канала (см. llm.summarize_digest); если не передана или
    пустая — используется нейтральный заголовок. Дайджест может уходить и
    днём, и утром (см. config.DIGEST_FLUSH_HOURS_MSK) — заголовок по
    умолчанию не привязан к времени суток.

    30.09.2026 — формат отдельной новости внутри дайджеста сменился с
    "блока" (жирный заголовок + комментарий + источник, три строки) на
    одну строку (_digest_line) — список читается быстрее, чем несколько
    заголовков подряд. Разделитель между новостями соответственно облегчён
    (было "———" между блоками, подходило для трёх строк; для списка из
    одной строки на новость это визуально слишком тяжело) — теперь просто
    перенос строки, без декоративного разделителя."""
    blocks = [
        _digest_line(it["source"], it["comment_ru"], it["link"], it.get("topic_tag"))
        for it in queue_items
    ]

    divider = "\n"
    intro = _esc(intro_ru) if intro_ru else "Что вы пропустили"

    def header(part_no: int, total: int) -> str:
        text = intro if part_no == 1 else "Продолжение дайджеста"
        suffix = f" ({part_no}/{total})" if total > 1 else ""
        return f"<b>{text}{suffix}:</b>\n\n"

    # раскладываем блоки по частям так, чтобы каждая часть влезала в лимит;
    # номер части в заголовке узнаем только после того, как разложили всё —
    # поэтому сначала считаем без заголовка (с запасом на него), потом
    # проставляем финальные заголовки с правильным total. Запас берём по
    # худшему из двух вариантов заголовка (интро может быть длиннее, чем
    # "Продолжение ночного дайджеста", если модель написала фразу подлиннее)
    # и с большим total "про запас", чтобы "(N/99)" точно не короче реального.
    header_budget = max(len(header(1, 99)), len(header(2, 99)))
    parts: list = [[]]
    current_len = header_budget
    for block in blocks:
        add_len = len(block) + (len(divider) if parts[-1] else 0)
        if parts[-1] and current_len + add_len > TELEGRAM_TEXT_LIMIT:
            parts.append([])
            current_len = header_budget
        parts[-1].append(block)
        current_len += add_len

    total = len(parts)
    return [header(i, total) + divider.join(part_blocks) for i, part_blocks in enumerate(parts, start=1)]


def send_message(
    text: str,
    silent: bool = False,
    disable_preview: bool = False,
    chat_id: str | None = None,
    parse_mode: str | None = "HTML",
) -> bool:
    """silent=True шлёт сообщение без звука/вибрации у подписчиков
    (Telegram disable_notification) — сам пост при этом появляется в канале
    сразу же, ничего не задерживается. Используется ночью (23:00–08:00 МСК,
    см. timeutil.is_night_msk), чтобы не будить подписчиков уведомлением,
    но и не жертвовать своевременностью для тех, кто открывает канал сам.

    disable_preview=True отключает предпросмотр ссылки под сообщением
    (Telegram disable_web_page_preview). Изначально (24.09.2026) это было
    только для дайджеста: там в ОДНОМ сообщении сразу несколько разных
    ссылок (по одной на каждую новость), а Telegram рендерит превью только
    для первой найденной ссылки в тексте — то есть подписчик видел превью
    только последней добавленной в дайджест новости, как будто весь пост
    про неё одну. 01.10.2026, решение автора — то же самое (disable_preview=
    True) теперь и у одиночных "громких" постов (main.py, ветка
    urgency == "breaking"): превью под постом визуально отвлекает от текста
    и дублирует то, что и так сказано словами, а не ошибка рендера, как у
    дайджеста — но решение то же, разные причины.

    26.09.2026, вечер — `chat_id`/`parse_mode` необязательны, по умолчанию
    не меняют поведение (chat_id=None → config.TELEGRAM_CHAT_ID, как и
    раньше; parse_mode="HTML", как и раньше). Нужны scripts/issue_v2.py
    (минимальный запуск v2, решение менеджера): выпуск и разбор ошибок
    фактчекера уходят не в канал, а в личку автору (chat_id=
    config.TELEGRAM_AUTHOR_CHAT_ID), причём разбор ошибок — обычным
    текстом, без HTML (parse_mode=None) — это отчёт для автора, а не пост
    в канал, там нет смысла рисковать HTML-эскейпингом текста, который сам
    же Фактчекер написал в свободной форме (`note` в claims)."""
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id if chat_id is not None else config.TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": disable_preview,
        "disable_notification": silent,
    }
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    try:
        resp = requests.post(url, json=payload, timeout=config.REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.error("Telegram API вернул ошибку %s: %s", resp.status_code, resp.text)
            return False
        return True
    except Exception as exc:
        logger.error("Не удалось отправить сообщение в Telegram: %s", exc)
        return False
