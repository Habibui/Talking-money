"""
Хранение уже опубликованных новостей — чтобы не постить одно и то же дважды.

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
