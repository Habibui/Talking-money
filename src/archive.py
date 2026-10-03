"""
Хранение архива v2: извлечённые заметки (Экстрактор) и опубликованные
выпуски (после Аналитика/Фактчекера) — читает и пишет их Сборщик/Отборщик/
Аналитик. Отдельно от src/state.py (v1) намеренно: другой формат (JSONL,
не единый JSON-объект) и другая семантика — здесь не "что уже опубликовано",
а "что уже разобрано на факты" и "что уже вышло выпуском".

Формат archive/cards/YYYY-MM-DD.jsonl — по одной JSON-строке на заметку:
{
  "id": "<hash>",            // см. compute_card_id() — по source+link
  "source": "...",
  "link": "...",
  "language": "en" | "ru",
  "extracted_at": "<ISO 8601 UTC>",
  ...весь JSON-ответ Экстрактора (topics/region/actors/key_facts/importance/
  importance_reason/market_link/content_level/tags_en)...
}

Формат archive/issues.jsonl — по одной JSON-строке на выпуск:
{
  "issue_id": "...",
  "published_at": "<ISO 8601 UTC>",
  "story_titles": [...],     // заголовки sujetов этого выпуска (для
                              // Отборщика, шаг 4 — проверка повтора)
  "draft": {...}             // финальный (после фактчека) JSON выпуска
}
"""

import glob
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from . import config

logger = logging.getLogger(__name__)


def _day_path(day: str) -> str:
    return os.path.join(config.ARCHIVE_CARDS_DIR, f"{day}.jsonl")


def append_card(card: dict) -> None:
    """card — dict с ключами id/source/link/language/extracted_at + весь
    ответ Экстрактора. Пишется в файл по дню extracted_at (UTC)."""
    day = card["extracted_at"][:10]  # "2026-09-26T12:34:56+00:00" -> "2026-09-26"
    os.makedirs(config.ARCHIVE_CARDS_DIR, exist_ok=True)
    with open(_day_path(day), "a", encoding="utf-8") as f:
        f.write(json.dumps(card, ensure_ascii=False))
        f.write("\n")


def load_cards_since(hours: int) -> list[dict]:
    """Возвращает все заметки за последние `hours` часов (по extracted_at),
    из всех дневных файлов, которые формально могут попасть в это окно (день
    запуска и, на случай окна >24ч или запуска сразу после полуночи UTC,
    предыдущий день). Порядок — по возрастанию extracted_at."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    days_to_check = set()
    d = cutoff
    now = datetime.now(timezone.utc)
    while d.date() <= now.date():
        days_to_check.add(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    cards = []
    for day in sorted(days_to_check):
        path = _day_path(day)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    card = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Повреждённая строка в %s, пропускаем: %r", path, line[:200])
                    continue
                try:
                    extracted_at = datetime.fromisoformat(card["extracted_at"])
                except (KeyError, ValueError):
                    continue
                if extracted_at >= cutoff:
                    cards.append(card)

    cards.sort(key=lambda c: c["extracted_at"])
    return cards


def load_all_card_files() -> list[str]:
    """Служебное — список всех дневных файлов архива заметок (для
    scripts/dry_run_v2.py и ручного разбора), новые сначала."""
    if not os.path.isdir(config.ARCHIVE_CARDS_DIR):
        return []
    return sorted(glob.glob(os.path.join(config.ARCHIVE_CARDS_DIR, "*.jsonl")), reverse=True)


def append_issue(issue_record: dict) -> None:
    """issue_record — {issue_id, published_at, story_titles, draft}."""
    os.makedirs(os.path.dirname(config.ARCHIVE_ISSUES_PATH), exist_ok=True)
    with open(config.ARCHIVE_ISSUES_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(issue_record, ensure_ascii=False))
        f.write("\n")


def load_last_issues(n: int) -> list[dict]:
    """Последние n выпусков (по порядку файла — он append-only, так что
    последние строки файла и есть последние по времени выпуски). Пустой
    список, если файла ещё нет (первый запуск v2 — нормальный случай, не
    ошибка)."""
    if not os.path.exists(config.ARCHIVE_ISSUES_PATH):
        return []
    with open(config.ARCHIVE_ISSUES_PATH, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    issues = []
    for line in lines[-n:]:
        try:
            issues.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Повреждённая строка в %s, пропускаем", config.ARCHIVE_ISSUES_PATH)
    return issues


def load_last_issue_titles(n: int) -> list[str]:
    """Заголовки сюжетов последних n выпусков, единым списком (без разбивки
    по выпускам — Отборщику для шага 4 достаточно самого списка заголовков,
    см. format-v2-prompts-draft.md, «Вход» раздела Отборщика)."""
    titles = []
    for issue in load_last_issues(n):
        titles.extend(issue.get("story_titles", []))
    return titles


def _strip_tags(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text or "").strip()


def load_last_issue_summaries(n: int, max_what_chars: int = 220) -> list[str]:
    """03.10.2026 — для шага 4 Отборщика (повтор темы): по каждому из
    последних n выпусков — дата, затем заголовки блоков и начало «что
    произошло» (там живут конкретные числа, по которым видно, что это ТА ЖЕ
    история). Одних заголовков не хватило: тема доходностей облигаций прошла
    три выпуска подряд под тремя разными формулировками, и по заголовкам
    Отборщик не узнавал в ней повтор. Возвращает плоский список строк,
    старые выпуски первыми; выпуски без draft (старый формат) — только по
    story_titles."""
    out = []
    for issue in load_last_issues(n):
        date = (issue.get("published_at") or "")[:10]
        blocks = (issue.get("draft") or {}).get("blocks") or []
        if not blocks:
            for t in issue.get("story_titles", []):
                out.append(f"[{date}] {t}")
            continue
        for b in blocks:
            title = _strip_tags(b.get("title", ""))
            what = _strip_tags(b.get("what", ""))
            if len(what) > max_what_chars:
                what = what[:max_what_chars].rsplit(" ", 1)[0] + "…"
            out.append(f"[{date}] {title} — {what}")
    return out


_URL_DATE_RE = None


def note_published_at(card: dict) -> datetime | None:
    """Лучшая оценка момента ПУБЛИКАЦИИ новости у источника: поле
    published_at (с 03.10.2026 пишется Сборщиком), иначе дата из URL вида
    /2026/09/29/ (CNBC и ряд других ее кладут в ссылку — так отлавливаются и
    старые заметки архива, у которых published_at ещё нет), иначе None
    (тогда вызывающий код опирается на extracted_at)."""
    global _URL_DATE_RE
    import re
    raw = card.get("published_at")
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    if _URL_DATE_RE is None:
        _URL_DATE_RE = re.compile(r"/(20\d\d)/(\d{2})/(\d{2})/")
    m = _URL_DATE_RE.search(card.get("link", ""))
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def note_age_hours(card: dict, now: datetime | None = None) -> float:
    """Возраст новости в часах. Для даты из URL (только день) берём конец
    этого дня — чтобы не отсечь заметку за «вчера вечером» из-за потери
    времени суток."""
    now = now or datetime.now(timezone.utc)
    published = note_published_at(card)
    if published is not None:
        if not card.get("published_at"):  # только дата из URL — берём конец дня
            published = published + timedelta(hours=23, minutes=59)
        return max(0.0, (now - published).total_seconds() / 3600)
    try:
        extracted = datetime.fromisoformat(card["extracted_at"])
        if extracted.tzinfo is None:
            extracted = extracted.replace(tzinfo=timezone.utc)
        return max(0.0, (now - extracted).total_seconds() / 3600)
    except (KeyError, ValueError):
        return 0.0


def filter_fresh(cards: list[dict], max_age_hours: float, now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """(свежие, отброшенные как устаревшие)."""
    fresh, stale = [], []
    for c in cards:
        (fresh if note_age_hours(c, now) <= max_age_hours else stale).append(c)
    return fresh, stale


# --- Журнал уже извлечённых URL (Сборщик) ------------------------------------
# Отдельно от load_state()/save_state() в state.py (v1) — см. комментарий у
# config.V2_SEEN_PATH про то, почему это не общий с v1 файл.


def compute_card_id(source: str, link: str) -> str:
    import hashlib
    raw = f"{source}::{link}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def load_seen() -> set[str]:
    if not os.path.exists(config.V2_SEEN_PATH):
        return set()
    with open(config.V2_SEEN_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return set(data.get("ids", []))


def save_seen(ids: set[str]) -> None:
    trimmed = list(ids)[-config.V2_MAX_SEEN_IDS:]
    os.makedirs(os.path.dirname(config.V2_SEEN_PATH), exist_ok=True)
    with open(config.V2_SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump({"ids": trimmed}, f, ensure_ascii=False, indent=2)
        f.write("\n")
