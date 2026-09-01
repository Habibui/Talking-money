"""
Получение свежих заголовков из источников: RSS-фиды и публичный
веб-предпросмотр телеграм-канала (t.me/s/<channel>) для Bloomberg.
"""

import html
import logging
import re

import feedparser
import requests
from bs4 import BeautifulSoup

from . import config

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; talkuyut-dengi-bot/1.0; "
    "+https://t.me/talkuyut_dengi)"
}


def _clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)  # на случай HTML внутри summary
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_rss(source: dict) -> list:
    """Возвращает список {source, title, summary, link} из RSS-фида."""
    items = []
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as exc:
        logger.error("Не удалось получить RSS %s (%s): %s", source["name"], source["url"], exc)
        return items

    for entry in feed.entries[: config.MAX_ITEMS_PER_SOURCE_PER_RUN]:
        title = _clean_text(getattr(entry, "title", ""))
        summary = _clean_text(getattr(entry, "summary", "") or getattr(entry, "description", ""))
        link = getattr(entry, "link", "")
        if not title or not link:
            continue
        items.append(
            {
                "source": source["name"],
                "title": title,
                "summary": summary,
                "link": link,
            }
        )
    return items


def fetch_telegram_channel(source: dict) -> list:
    """Возвращает список {source, title, summary, link} из публичного
    превью телеграм-канала (без авторизации, без Telegram API)."""
    channel = source["telegram_channel"]
    url = f"https://t.me/s/{channel}"
    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Не удалось получить телеграм-канал %s: %s", channel, exc)
        return items

    soup = BeautifulSoup(resp.text, "html.parser")
    messages = soup.select("div.tgme_widget_message")[-config.MAX_ITEMS_PER_SOURCE_PER_RUN :]

    for msg in messages:
        # пропускаем чисто медийные посты без текста
        text_div = msg.select_one(".tgme_widget_message_text")
        if not text_div:
            continue
        text = _clean_text(text_div.get_text(" "))
        if not text:
            continue

        # ссылка на конкретное сообщение канала — она же уникальный id
        link_tag = msg.select_one("a.tgme_widget_message_date")
        link = link_tag["href"] if link_tag and link_tag.has_attr("href") else url

        # заголовок отдельно RSS не даёт — используем первые ~120 символов
        # текста как "заголовок", остальное как summary
        title = text if len(text) <= 120 else text[:117].rsplit(" ", 1)[0] + "…"
        summary = text

        items.append(
            {
                "source": source["name"],
                "title": title,
                "summary": summary,
                "link": link,
            }
        )
    return items


def fetch_all() -> list:
    all_items = []
    for source in config.SOURCES:
        if source["type"] == "rss":
            all_items.extend(fetch_rss(source))
        elif source["type"] == "telegram":
            all_items.extend(fetch_telegram_channel(source))
        else:
            logger.warning("Неизвестный тип источника: %s", source)
    return all_items
