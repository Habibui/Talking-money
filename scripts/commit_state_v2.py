#!/usr/bin/env python3
"""
26.09.2026, вечер — коммитит и пушит state/v2_seen.json и новые/изменённые
archive/cards/*.jsonl + archive/issues.jsonl после запуска
scripts/collect_v2.py или scripts/issue_v2.py, устойчиво к гонке push —
ОТДЕЛЬНЫЙ скрипт от scripts/commit_state.py (v1), не переиспользует его
код напрямую: у v2 другая форма файлов (построчный JSONL + множество
date-partitioned файлов в archive/cards/, а не фиксированный список
JSON-объектов), поэтому и слияние другое. ОБЩАЯ СХЕМА (коммит -> если push
не прошёл -> fetch + слить по смыслу -> reset --soft на свежий origin ->
коммит заново -> повторить, до MAX_ATTEMPTS раз) взята буквально из
commit_state.py — тот же класс проблемы (два запуска пушат почти
одновременно), тот же по духу фикс, уже проверенный на практике этим же
пайплайном (см. её собственный docstring про дубль поста Texas Stock
Exchange 10.09.2026).

Слияние:
- state/v2_seen.json — как posted.json у v1: множество id, объединяем как
  множества (порядок не важен), обрезаем до V2_MAX_SEEN_IDS.
- archive/cards/*.jsonl и archive/issues.jsonl — построчный JSON,
  объединяем как множество СТРОК (не парсим построчно) — сама строка
  целиком уже уникально идентифицирует запись, порядок внутри файла не
  важен ни Отборщику, ни Аналитику (читают файл целиком, не по позиции).

Известный, ПРИНЯТЫЙ (не случайно упущенный) остаточный риск: если два
Сборщика реально пересекутся по времени (что при concurrency:
cancel-in-progress: true в .github/workflows/publish_v2.yml не должно
происходить в норме — см. комментарий там же, тот же принцип, что и у
publish.yml v1) и независимо разберут одну и ту же статью, в архиве
останутся ДВЕ строки с одним id, но разным extracted_at — построчный
дедуп по точному совпадению строки это не поймает (строки не идентичны).
Не устраняем это здесь намеренно: правильная защита — не дать самим
запускам пересекаться (concurrency в workflow), а не гоняться за
построчным JSON-дедупом ради сценария, которого и так не должно быть;
безвредный дубль в архиве (не двойная ПУБЛИКАЦИЯ, не показывается
читателю) — приемлемая цена простоты."""

import json
import random
import subprocess
import sys
import time

V2_JSON_STATE_FILES = ["state/v2_seen.json"]
V2_ISSUES_PATH = "archive/issues.jsonl"
V2_MAX_SEEN_IDS = 5000  # держать в синхроне с config.V2_MAX_SEEN_IDS

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


def load_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f if line.strip()]
    except FileNotFoundError:
        return None


def save_lines(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def git_show_text(ref, path):
    result = run(["git", "show", f"{ref}:{path}"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout


def changed_jsonl_paths():
    """archive/cards/*.jsonl с реальными изменениями в рабочем дереве
    (новые или изменённые файлы) плюс archive/issues.jsonl, если он
    существует (проверяется на «изменился ли» позже, через
    git diff --cached --quiet — здесь достаточно включить его в
    кандидаты)."""
    status = run(["git", "status", "--porcelain", "archive/cards/"], check=False).stdout
    paths = []
    for line in status.splitlines():
        path = line[3:].strip()
        if path and path not in paths:
            paths.append(path)
    if load_lines(V2_ISSUES_PATH) is not None and V2_ISSUES_PATH not in paths:
        paths.append(V2_ISSUES_PATH)
    return paths


def merge_seen(mine, theirs):
    if theirs is None:
        return mine
    if mine is None:
        return theirs
    merged_ids = list(dict.fromkeys(theirs.get("ids", []) + mine.get("ids", [])))
    return {"ids": merged_ids[-V2_MAX_SEEN_IDS:]}


def merge_jsonl_union(mine_lines, theirs_text):
    theirs_lines = [line for line in (theirs_text or "").splitlines() if line.strip()]
    if mine_lines is None:
        mine_lines = []
    return list(dict.fromkeys(theirs_lines + mine_lines))


def merge_state_with_origin(branch, jsonl_paths):
    for path in V2_JSON_STATE_FILES:
        mine = load_json(path)
        theirs_raw = git_show_text(f"origin/{branch}", path)
        theirs = json.loads(theirs_raw) if theirs_raw else None
        merged = merge_seen(mine, theirs)
        if merged is not None:
            save_json(path, merged)

    for path in jsonl_paths:
        mine_lines = load_lines(path)
        theirs_text = git_show_text(f"origin/{branch}", path)
        merged_lines = merge_jsonl_union(mine_lines, theirs_text)
        if merged_lines:
            save_lines(path, merged_lines)


def stage_existing(paths):
    for path in paths:
        run(["git", "add", path], check=False)


def main() -> int:
    branch = sys.argv[1] if len(sys.argv) > 1 else "main"

    run(["git", "config", "user.name", "talkuyut-dengi-bot"])
    run(["git", "config", "user.email", "actions@users.noreply.github.com"])

    jsonl_paths = changed_jsonl_paths()
    all_paths = V2_JSON_STATE_FILES + jsonl_paths
    stage_existing(all_paths)

    if run(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
        print("Нет изменений в v2-состоянии/архиве — коммитить нечего")
        return 0

    run(["git", "commit", "-m", "chore(v2): обновить архив/state [skip ci]"])

    for attempt in range(1, MAX_ATTEMPTS + 1):
        push = run(["git", "push", "origin", f"HEAD:{branch}"], check=False)
        if push.returncode == 0:
            print(f"push успешен (попытка {attempt})")
            return 0

        print(f"push не прошёл (попытка {attempt}/{MAX_ATTEMPTS}): {push.stderr.strip()}")
        if attempt == MAX_ATTEMPTS:
            break

        run(["git", "fetch", "origin", branch], check=True)
        merge_state_with_origin(branch, jsonl_paths)
        stage_existing(all_paths)

        run(["git", "reset", "--soft", f"origin/{branch}"], check=True)
        stage_existing(all_paths)

        if run(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
            print("После слияния изменений не осталось — конкурентный запуск уже всё учёл")
            return 0

        run(["git", "commit", "-m", "chore(v2): обновить архив/state [skip ci]"])
        time.sleep(random.uniform(1, 5))

    print(f"Не удалось запушить v2-состояние после {MAX_ATTEMPTS} попыток")
    return 1


if __name__ == "__main__":
    sys.exit(main())
