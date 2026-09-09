"""
Хранение состояния пайплайна: что уже опубликовано (дедуп по id), очередь
рутинных новостей на ближайший дайджест, и недавние посты (для смысловой
проверки на дубли/апдейты между разными источниками).

Формат state/posted.json:
{
  "ids": ["<hash1>", "<hash2>", ...],   // последние MAX_STATE_IDS id
  "bootstrapped": true                   // false/отсутствует — значит,
                                          // это первый запуск и постить пока
                                          // ничего не надо, только запомнить
                                          // текущие заголовки
}
"""

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

from . import config


def compute_id(source_name: str, link: str) -> str:
    raw = f"{source_name}::{link}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def load_state() -> dict:
    if not os.path.exists(config.STATE_PATH):
        return {"ids": [], "bootstrapped": False}
    with open(config.STATE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("ids", [])
    data.setdefault("bootstrapped", False)
    return data


def save_state(state: dict) -> None:
    # обрезаем до последних MAX_STATE_IDS, чтобы файл не рос бесконечно
    state["ids"] = state["ids"][-config.MAX_STATE_IDS:]
    os.makedirs(os.path.dirname(config.STATE_PATH), exist_ok=True)
    with open(config.STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def filter_new_items(state: dict, items: list) -> list:
    """items — список dict с ключами source/title/link/summary.
    Возвращает только те, чей id ещё не встречался."""
    seen = set(state["ids"])
    new_items = []
    for item in items:
        item_id = compute_id(item["source"], item["link"])
        item["id"] = item_id
        if item_id not in seen:
            new_items.append(item)
    return new_items


def mark_posted(state: dict, items: list) -> None:
    for item in items:
        item_id = item.get("id") or compute_id(item["source"], item["link"])
        state["ids"].append(item_id)


# --- Очередь рутинных новостей (для ближайшего дайджеста) --------------------
# Отдельный файл (не posted.json) — так дедуп по id и содержимое дайджеста не
# смешиваются, и очередь можно спокойно очистить, не трогая историю id.


def load_digest_queue() -> list:
    if not os.path.exists(config.DIGEST_QUEUE_PATH):
        return []
    with open(config.DIGEST_QUEUE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_digest_queue(queue: list) -> None:
    os.makedirs(os.path.dirname(config.DIGEST_QUEUE_PATH), exist_ok=True)
    with open(config.DIGEST_QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)
        f.write("\n")


def append_to_digest_queue(source: str, headline_ru: str, comment_ru: str, link: str) -> None:
    queue = load_digest_queue()
    queue.append({
        "source": source,
        "headline_ru": headline_ru,
        "comment_ru": comment_ru,
        "link": link,
    })
    save_digest_queue(queue)


# --- Метаданные дайджеста (когда флашили в последний раз) --------------------
# Нужно, чтобы за один и тот же час (например, 12:00-12:59, за который пайплайн
# успеет отработать 3-4 раза) дайджест ушёл ровно один раз, а не при каждом
# запуске внутри этого часа.


def load_digest_meta() -> dict:
    if not os.path.exists(config.DIGEST_META_PATH):
        return {"last_flush_key": None}
    with open(config.DIGEST_META_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("last_flush_key", None)
    return data


def save_digest_meta(meta: dict) -> None:
    os.makedirs(os.path.dirname(config.DIGEST_META_PATH), exist_ok=True)
    with open(config.DIGEST_META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --- Недавние посты (контекст для проверки на дубли/апдейты) -----------------
# Не только заголовок+ссылка, но и уже написанный комментарий — в нём уже
# сжаты ключевые факты (см. системный промпт llm.py), этого достаточно модели,
# чтобы понять, действительно ли новая новость добавляет что-то новое, или это
# то же самое, что уже публиковали (пусть и с другого источника).


def load_recent_posts() -> list:
    if not os.path.exists(config.RECENT_POSTS_PATH):
        return []
    with open(config.RECENT_POSTS_PATH, "r", encoding="utf-8") as f:
        posts = json.load(f)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.RECENT_POSTS_MAX_AGE_HOURS)
    fresh = [p for p in posts if datetime.fromisoformat(p["posted_at"]) >= cutoff]
    return fresh[-config.RECENT_POSTS_MAX_COUNT:]


def save_recent_posts(posts: list) -> None:
    posts = posts[-config.RECENT_POSTS_MAX_COUNT:]
    os.makedirs(os.path.dirname(config.RECENT_POSTS_PATH), exist_ok=True)
    with open(config.RECENT_POSTS_PATH, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)
        f.write("\n")


def append_recent_post(posts: list, source: str, headline_ru: str, comment_ru: str) -> list:
    """Добавляет запись в переданный в память список (не перечитывает файл —
    вызывающий код сам ведёт posts в течение всего прогона, чтобы более ранние
    новости этого же запуска тоже участвовали в сравнении для более поздних)
    и сразу сохраняет на диск. Возвращает обновлённый список."""
    posts = posts + [{
        "source": source,
        "headline_ru": headline_ru,
        "comment_ru": comment_ru,
        "posted_at": datetime.now(timezone.utc).isoformat(),
    }]
    save_recent_posts(posts)
    return posts
