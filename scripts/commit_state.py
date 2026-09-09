#!/usr/bin/env python3
"""
Коммитит и пушит обновлённое state/*.json после запуска пайплайна, устойчиво
к гонке с параллельным запуском workflow.

Без этого: если два запуска почти одновременно пытаются запушить обновлённый
state, проигравший получает "rejected (fetch first)" и падает с ошибкой —
но Telegram-пост к этому моменту УЖЕ отправлен (это происходит раньше, в
main.py, до этого шага), просто его id не попадает в posted.json. Следующий
запуск видит этот заголовок как "ещё не опубликованный" и публикует его
повторно. Именно так возник дубль поста про Texas Stock Exchange в ночь на
10.09.2026 — сразу после деплоя новой версии, когда два запуска пересеклись
по времени.

При неудачном push: подтягиваем свежий origin и СЛИВАЕМ state-файлы по
смыслу (объединяем множества id/записей), а не берём чью-то версию целиком —
иначе можно было бы точно так же потерять то, что успел записать
конкурентный запуск. Повторяем до MAX_ATTEMPTS раз.
"""

import json
import random
import subprocess
import sys
import time

STATE_FILES = [
    "state/posted.json",
    "state/digest_queue.json",
    "state/digest_meta.json",
    "state/recent_posts.json",
]

MAX_STATE_IDS = 3000
MAX_ATTEMPTS = 5


def run(cmd, check=True):
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def git_show(ref, path):
    result = run(["git", "show", f"{ref}:{path}"], check=False)
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def merge_posted(mine, theirs):
    if theirs is None:
        return mine
    if mine is None:
        return theirs
    merged_ids = list(dict.fromkeys(theirs.get("ids", []) + mine.get("ids", [])))
    return {"ids": merged_ids[-MAX_STATE_IDS:], "bootstrapped": True}


def merge_list_by_key(mine, theirs, key_fn, sort_key=None):
    """Объединяет два списка dict'ов, убирая дубли по key_fn. Порядок важен:
    ключ должен реально различать разные записи — например, в
    digest_queue.json есть "link" (уникален на новость), а в
    recent_posts.json его нет (там за уникальность отвечает
    source+headline_ru), поэтому ключ передаётся отдельно для каждого файла,
    а не жёстко зашит здесь."""
    if theirs is None:
        return mine or []
    if mine is None:
        return theirs
    seen = set()
    merged = []
    for item in theirs + mine:
        key = key_fn(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    if sort_key:
        merged.sort(key=sort_key)
    return merged


def merge_digest_meta(mine, theirs):
    if theirs is None:
        return mine
    if mine is None:
        return theirs
    mine_key = mine.get("last_flush_key") or ""
    theirs_key = theirs.get("last_flush_key") or ""
    return {"last_flush_key": (max(mine_key, theirs_key) or None)}


def merge_state_with_origin(branch):
    """Перечитывает текущие (наши) файлы с диска, сравнивает с версией на
    origin/<branch> и сохраняет слитый результат обратно на диск."""
    mine = {p: load_json(p) for p in STATE_FILES}
    theirs = {p: git_show(f"origin/{branch}", p) for p in STATE_FILES}

    merged = {
        "state/posted.json": merge_posted(
            mine["state/posted.json"], theirs["state/posted.json"]
        ),
        "state/digest_queue.json": merge_list_by_key(
            mine["state/digest_queue.json"],
            theirs["state/digest_queue.json"],
            key_fn=lambda it: (it.get("source"), it.get("link")),
        ),
        "state/recent_posts.json": merge_list_by_key(
            mine["state/recent_posts.json"],
            theirs["state/recent_posts.json"],
            key_fn=lambda it: (it.get("source"), it.get("headline_ru"), it.get("posted_at")),
            sort_key=lambda it: it.get("posted_at", ""),
        ),
        "state/digest_meta.json": merge_digest_meta(
            mine["state/digest_meta.json"], theirs["state/digest_meta.json"]
        ),
    }

    for path, data in merged.items():
        if data is not None:
            save_json(path, data)


def stage_existing(paths):
    for path in paths:
        if load_json(path) is not None:
            run(["git", "add", path], check=False)


def main() -> int:
    branch = sys.argv[1] if len(sys.argv) > 1 else "main"

    run(["git", "config", "user.name", "talkuyut-dengi-bot"])
    run(["git", "config", "user.email", "actions@users.noreply.github.com"])

    stage_existing(STATE_FILES)

    if run(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
        print("Нет изменений в state — коммитить нечего")
        return 0

    run(["git", "commit", "-m", "chore: обновить state [skip ci]"])

    for attempt in range(1, MAX_ATTEMPTS + 1):
        push = run(["git", "push", "origin", f"HEAD:{branch}"], check=False)
        if push.returncode == 0:
            print(f"push успешен (попытка {attempt})")
            return 0

        print(f"push не прошёл (попытка {attempt}/{MAX_ATTEMPTS}): {push.stderr.strip()}")
        if attempt == MAX_ATTEMPTS:
            break

        run(["git", "fetch", "origin", branch], check=True)
        merge_state_with_origin(branch)
        stage_existing(STATE_FILES)

        # переносим наши изменения поверх свежего origin, чтобы коммит не
        # тянул за собой устаревшего родителя (иначе push снова будет rejected)
        run(["git", "reset", "--soft", f"origin/{branch}"], check=True)
        stage_existing(STATE_FILES)

        if run(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
            print("После слияния изменений не осталось — конкурентный запуск уже всё учёл")
            return 0

        run(["git", "commit", "-m", "chore: обновить state [skip ci]"])
        time.sleep(random.uniform(1, 5))

    print(f"Не удалось запушить state после {MAX_ATTEMPTS} попыток")
    return 1


if __name__ == "__main__":
    sys.exit(main())
