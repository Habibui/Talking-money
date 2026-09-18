#!/usr/bin/env python3
"""
19.09.2026. Печатает журнал пограничных случаев дедупа
(state/dedup_escalations.json — см. src/state.py, main.py) в компактном виде
для быстрого ручного разбора: "был ли этот конкретный ответ модели
(confirm_same_event) верным?".

Это НЕ автоматическая проверка — правильность ответа модели ("это правда
одно и то же событие или нет") может оценить только человек, прочитавший оба
текста, поэтому сама оценка тут не автоматизируется. Автоматизирован только
сбор данных для неё: раньше для этого нужно было вручную гонять
`gh run view --log` по десяткам запусков GitHub Actions (с ограниченным
сроком хранения логов) — теперь достаточно этого файла, который переживает
запуски.

Запуск:
  python3 scripts/review_escalations.py            # все записи
  python3 scripts/review_escalations.py 7          # только за последние 7 дней
  python3 scripts/review_escalations.py --only=yes # только "ДА" (посчитано дублем)
  python3 scripts/review_escalations.py --only=no  # только "НЕТ"
  python3 scripts/review_escalations.py --only=err # только сбой проверки (None)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config

VERDICT_LABELS = {
    True: "ДА, дубль",
    False: "нет, не дубль",
    None: "СБОЙ проверки (сеть/API) — опубликовано как обычно",
}


def load_entries() -> list:
    if not os.path.exists(config.DEDUP_ESCALATIONS_PATH):
        return []
    with open(config.DEDUP_ESCALATIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_args(argv: list) -> tuple[int | None, str | None]:
    """Возвращает (сколько_дней_назад или None, фильтр_only или None)."""
    days = None
    only = None
    for arg in argv:
        if arg.startswith("--only="):
            only = arg.split("=", 1)[1].strip().lower()
        elif arg.isdigit():
            days = int(arg)
    return days, only


def matches_only(entry: dict, only: str | None) -> bool:
    if only is None:
        return True
    same_event = entry.get("same_event")
    if only == "yes":
        return same_event is True
    if only == "no":
        return same_event is False
    if only == "err":
        return same_event is None
    print(f"Неизвестный фильтр --only={only!r} (ожидается yes/no/err) — игнорирую фильтр")
    return True


def format_entry(i: int, entry: dict) -> str:
    ts_raw = entry.get("timestamp", "")
    try:
        ts = datetime.fromisoformat(ts_raw).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        ts = ts_raw or "?"

    verdict = VERDICT_LABELS.get(entry.get("same_event"), str(entry.get("same_event")))

    lines = [
        f"[{i}] {ts}  jaccard={entry.get('jaccard')}  значимых_токенов={entry.get('salient_overlap')}"
        f"  ответ модели: {verdict}",
        f"    [{entry.get('new_source', '?')}] {entry.get('new_headline_ru', '')}",
        f"    [{entry.get('old_source', '?')}] {entry.get('old_headline_ru', '')}",
    ]
    return "\n".join(lines)


def main() -> int:
    days, only = parse_args(sys.argv[1:])
    entries = load_entries()

    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        entries = [
            e for e in entries
            if e.get("timestamp") and datetime.fromisoformat(e["timestamp"]) >= cutoff
        ]

    entries = [e for e in entries if matches_only(e, only)]

    if not entries:
        print("Пограничных случаев (подходящих под фильтр) в журнале нет.")
        return 0

    for i, entry in enumerate(entries, 1):
        print(format_entry(i, entry))
        print()

    counts = {"ДА": 0, "нет": 0, "сбой": 0}
    for e in entries:
        se = e.get("same_event")
        if se is True:
            counts["ДА"] += 1
        elif se is False:
            counts["нет"] += 1
        else:
            counts["сбой"] += 1
    print(
        f"Итого: {len(entries)} случаев — ДА: {counts['ДА']}, нет: {counts['нет']}, "
        f"сбой проверки: {counts['сбой']}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
