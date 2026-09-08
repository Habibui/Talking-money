"""
Публикация сообщения в канал через Telegram Bot API.
"""

import html
import logging

import requests

from . import config

logger = logging.getLogger(__name__)

# Лимит Telegram на длину text у sendMessage — 4096 символов (считая HTML-разметку
# в самой строке). Дайджест из нескольких новостей может в него не влезть —
# тогда режем на несколько сообщений подряд.
TELEGRAM_TEXT_LIMIT = 4096


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _news_block(source: str, headline_ru: str, comment_ru: str, link: str) -> str:
    headline = _esc(headline_ru)
    comment = _esc(comment_ru)
    source_esc = _esc(source)
    return (
        f"<b>{headline}</b>\n\n"
        f"{comment}\n\n"
        f'<a href="{link}">{source_esc} →</a>'
    )


def build_message(item: dict, translated: dict) -> str:
    return _news_block(item["source"], translated["headline_ru"], translated["comment_ru"], item["link"])


def build_digest_messages(queue_items: list) -> list:
    """Собирает накопленные за ночь новости в один пост (или несколько, если
    не влезает в лимит Telegram). queue_items — список dict с ключами
    source/headline_ru/comment_ru/link (см. state.append_to_night_queue)."""
    blocks = [
        _news_block(it["source"], it["headline_ru"], it["comment_ru"], it["link"])
        for it in queue_items
    ]

    divider = "\n\n———\n\n"

    def header(part_no: int, total: int) -> str:
        suffix = f" ({part_no}/{total})" if total > 1 else ""
        return f"<b>Пока вы спали{suffix}:</b>\n\n"

    # раскладываем блоки по частям так, чтобы каждая часть влезала в лимит;
    # номер части в заголовке узнаем только после того, как разложили всё —
    # поэтому сначала считаем без заголовка (с запасом на него), потом
    # проставляем финальные заголовки с правильным total
    header_budget = len(header(9, 9))  # с запасом, "(9/9)" длиннее реальных вариантов
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


def send_message(text: str) -> bool:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=config.REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.error("Telegram API вернул ошибку %s: %s", resp.status_code, resp.text)
            return False
        return True
    except Exception as exc:
        logger.error("Не удалось отправить сообщение в Telegram: %s", exc)
        return False
