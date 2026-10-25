"""
Политика очереди дайджеста (решение автора, 25.10.2026).

Проблема: очередь рутинных новостей копилась быстрее, чем уходила (вход
60–77 новостей в будни, дайджест вмещает ~12 × 4 в сутки), выросла до 300+
и дайджесты выходили с новостями многодневной давности.

На каждом флаше (main.maybe_flush_digest) перед сборкой дайджеста:
  1) срок жизни: важные новости (importance >= DIGEST_IMPORTANT_MIN) живут
     DIGEST_TTL_HOURS_IMPORTANT (3 суток), остальные — DIGEST_TTL_HOURS_ROUTINE
     (2 суток); просроченные удаляются;
  2) повторы: если в очереди несколько новостей об одном событии, остаётся
     самая свежая, более старые удаляются; важность выжившей поднимается до
     максимальной в группе (повтор — признак значимости);
  3) порядок и надбавки — в telegram_bot.build_digest_message: строго по
     важности, при равной — свежие вперёд, без надбавки за отсрочку.
"""

import logging
from datetime import datetime, timedelta, timezone

from . import config, dedup

logger = logging.getLogger(__name__)


def _importance(item: dict) -> int:
    return item.get("importance") or 3


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def stamp_queued_at(item: dict, now: datetime) -> dict:
    """Возвращает копию записи с гарантированным queued_at. У записей старого
    формата возраст оценивается по deferred_count (см.
    config.DIGEST_LEGACY_HOURS_PER_DEFER)."""
    item = dict(item)
    if _parse_ts(item.get("queued_at")) is None:
        hours = (item.get("deferred_count") or 0) * config.DIGEST_LEGACY_HOURS_PER_DEFER
        item["queued_at"] = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    return item


def is_expired(item: dict, now: datetime) -> bool:
    ttl_hours = (
        config.DIGEST_TTL_HOURS_IMPORTANT
        if _importance(item) >= config.DIGEST_IMPORTANT_MIN
        else config.DIGEST_TTL_HOURS_ROUTINE
    )
    queued = _parse_ts(item.get("queued_at")) or now
    return now - queued > timedelta(hours=ttl_hours)


def _default_confirm(new_text: str, old_text: str) -> bool | None:
    from . import llm  # локально: модуль llm тянет anthropic, тестам он не нужен
    return llm.confirm_same_event(new_text, old_text)


def prune_queue(queue: list, now: datetime | None = None, confirm=None,
                max_llm_checks: int | None = None) -> tuple[list, dict]:
    """Применяет срок жизни и схлопывает повторы. Возвращает (очередь в
    прежнем порядке — от старых к новым, статистика). Записи не мутируются."""
    now = now or datetime.now(timezone.utc)
    confirm = confirm or _default_confirm
    llm_budget = config.DIGEST_DEDUP_MAX_LLM_CHECKS if max_llm_checks is None else max_llm_checks
    stats = {"before": len(queue), "expired": 0, "duplicates": 0, "llm_checks": 0, "after": 0}

    stamped = [stamp_queued_at(it, now) for it in queue]
    alive = []
    for it in stamped:
        if is_expired(it, now):
            stats["expired"] += 1
        else:
            alive.append(it)

    # идём от свежих к старым: каждую запись сравниваем с уже оставленными
    # (более свежими); совпала — удаляем текущую (старую), а важность
    # свежей поднимаем до максимума в группе
    kept_rev: list = []        # оставленные, от свежих к старым
    kept_prep: list = []
    for it in reversed(alive):
        prep = dedup.prepare_text(it.get("headline_ru", ""), it.get("comment_ru", ""))
        dup_of = None
        ambiguous = []
        for pos, other_prep in enumerate(kept_prep):
            verdict, jaccard, _salient = dedup.classify_pair(prep, other_prep)
            if verdict == "duplicate":
                dup_of = pos
                break
            if verdict == "ambiguous":
                ambiguous.append((jaccard, pos))
        if dup_of is None and ambiguous:
            ambiguous.sort(reverse=True)
            for _jaccard, pos in ambiguous[: dedup.AMBIGUOUS_MAX_CANDIDATES]:
                if stats["llm_checks"] >= llm_budget:
                    break
                other = kept_rev[pos]
                stats["llm_checks"] += 1
                try:
                    same = confirm(
                        f"{other.get('headline_ru', '')} {other.get('comment_ru', '')}",
                        f"{it.get('headline_ru', '')} {it.get('comment_ru', '')}",
                    )
                except Exception:
                    logger.exception("Проверка повтора в очереди дайджеста упала — считаем, что не повтор")
                    same = None
                if same:
                    dup_of = pos
                    break
        if dup_of is None:
            kept_rev.append(it)
            kept_prep.append(prep)
        else:
            stats["duplicates"] += 1
            survivor = dict(kept_rev[dup_of])
            if _importance(it) > _importance(survivor):
                survivor["importance"] = it.get("importance")
            kept_rev[dup_of] = survivor

    kept = list(reversed(kept_rev))
    stats["after"] = len(kept)
    logger.info(
        "Очередь дайджеста: было %(before)d, просрочено %(expired)d, повторов %(duplicates)d "
        "(проверок модели: %(llm_checks)d), осталось %(after)d", stats,
    )
    return kept, stats
