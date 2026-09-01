"""
Публикация сообщения в канал через Telegram Bot API.
"""

import html
import logging

import requests

from . import config

logger = logging.getLogger(__name__)


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def build_message(item: dict, translated: dict) -> str:
    headline = _esc(translated["headline_ru"])
    comment = _esc(translated["comment_ru"])
    source = _esc(item["source"])
    link = item["link"]

    return (
        f"<b>{headline}</b>\n\n"
        f"{comment}\n\n"
        f'<a href="{link}">{source} →</a>'
    )


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
