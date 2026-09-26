"""
Сборщик (v2) — часовой прогон: забирает статьи из всех источников (как v1,
через src/sources.py — источники общие с v1, конфиг тот же config.SOURCES),
отсекает уже разобранные и явные/текстовые дубли, прогоняет новые через
Экстрактор и дописывает результат в архив (src/archive.py).

Дедуп — два слоя, оба перенесены из v1 "без архитектурных изменений" (см.
format-v2-prompts-draft.md, «Вход» у Отборщика):
1. Точный — по id (source+link), отдельный журнал config.V2_SEEN_PATH (не
   общий с v1 state/posted.json, см. комментарий там же).
2. Текстовое сходство — src/dedup.is_near_duplicate() против заметок,
   извлечённых за последнее время (см. _RECENT_WINDOW_HOURS ниже), тем же
   способом, что v1 использует для recent_posts. Оговорка, которой не было
   у v1: dedup.py тюнился на РУССКОМ тексте (стоп-слова, "значимые"
   слова-исключения — все русские, см. src/dedup.py), а большинство
   заметок v2 на английском (key_facts не переводится Экстрактором, см.
   правило 1 промпта). На английском тексте фильтр сработает грубее —
   стоп-слова не отсекутся, но само по себе сравнение по Jaccard всё равно
   ловит наиболее частый на практике случай (одно и то же событие, почти
   тем же текстом, у нескольких заметок ОДНОГО источника/агрегатора —
   см. src/dedup.py про Investing.com). Это осознанный компромисс на
   первую версию, не молчаливое допущение — если промах фильтра на
   практике окажется частым именно для en-заметок, дальше можно тюнить
   отдельно от русского словаря, а не трогать v1. Отборщик, группируя по
   смыслу через LLM, всё равно является основной защитой от дублей на
   уровне сюжета — этот слой снижает лишние вызовы Экстрактора, не более.
"""

import logging
from datetime import datetime, timezone

from . import archive, config, dedup, extractor, sources

logger = logging.getLogger(__name__)

# Окно сравнения на текстовое сходство — держим коротким (в отличие от 48ч
# у v1 recent_posts): Сборщик гоняется раз в час, цель тут — не тратить
# лишний вызов Экстрактора на статью, которую только что (в последний
# час-два) уже разобрали под другим источником, а не ловить дубли через
# сутки (это уже не задача Сборщика, а Отборщика на этапе группировки).
_RECENT_WINDOW_HOURS = 6


def _as_dedup_shape(card: dict) -> dict:
    """dedup.is_near_duplicate()/find_ambiguous_match() ожидают dict с
    headline_ru/comment_ru (см. src/dedup.py) — у v2-заметок таких полей
    нет, подставляем title/key_facts как ближайший эквивалент (см. docstring
    модуля про то, что это грубее для en-текста, но не бесполезно)."""
    return {
        "headline_ru": card.get("title", ""),
        "comment_ru": " ".join(card.get("key_facts", [])),
    }


def run(dry_run: bool = True) -> dict:
    """Один прогон Сборщика. dry_run — только для единообразия сигнатуры с
    остальными ролями v2 (Сборщик и так ничего не публикует в Telegram,
    только копит архив — публикация начинается у Аналитика/Фактчекера),
    оставлен на случай, если понадобится режим "не писать в архив, только
    посчитать, сколько бы записалось".

    Возвращает {"fetched": N, "skipped_seen": N, "skipped_duplicate": N,
    "extracted": N, "failed": N} — для лога/отчёта DRY_RUN."""
    stats = {
        "fetched": 0, "skipped_seen": 0, "skipped_duplicate": 0,
        "extracted": 0, "failed": 0,
    }

    items = sources.fetch_all()
    stats["fetched"] = len(items)

    seen_ids = archive.load_seen()
    recent_cards = archive.load_cards_since(_RECENT_WINDOW_HOURS)
    recent_shapes = [_as_dedup_shape(c) for c in recent_cards]

    for item in items:
        item_id = archive.compute_card_id(item["source"], item["link"])
        if item_id in seen_ids:
            stats["skipped_seen"] += 1
            continue

        # текстовое сходство сверяем по тому, что уже есть у сырого item —
        # summary из RSS/телеграма (title/summary), не дожидаясь Экстрактора,
        # чтобы не тратить вызов модели на то, что и так похоже на уже
        # разобранное в этом окне
        candidate_shape = {"headline_ru": item["title"], "comment_ru": item.get("summary", "")}
        is_dup, score, _matched = dedup.is_near_duplicate(
            candidate_shape["headline_ru"], candidate_shape["comment_ru"], recent_shapes
        )
        if is_dup:
            logger.info(
                "Сборщик: %s (%s) похож на уже разобранную заметку (score=%.2f), "
                "пропускаем без вызова Экстрактора", item["source"], item["link"], score,
            )
            stats["skipped_duplicate"] += 1
            seen_ids.add(item_id)  # чтобы не проверять его же снова на следующем часовом запуске
            continue

        article_text = sources.fetch_article_lead(item["link"])
        extractor_input = {
            "source": item["source"],
            "link": item["link"],
            "title": item["title"],
            "language": item.get("language", "en"),
            "article_text": article_text,
            "summary": item.get("summary", ""),
        }

        note = extractor.extract_note(extractor_input)
        seen_ids.add(item_id)  # помечаем разобранным независимо от успеха — повторная попытка
        # на ту же статью на следующем часовом прогоне почти наверняка снова
        # упрётся в ту же причину сбоя (403/пустой фид), не в мимолётную
        # сетевую ошибку; если ошибка и правда временная — заметка просто
        # не попадёт в архив на этот раз, что безопаснее, чем зависание на
        # одной проблемной статье при каждом запуске.
        if note is None:
            stats["failed"] += 1
            continue

        card = {
            "id": item_id,
            "source": item["source"],
            "link": item["link"],
            "language": item.get("language", "en"),
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            **note,
        }
        archive.append_card(card)
        recent_shapes.append(_as_dedup_shape(card))  # участвует в сравнении для следующих в этом же прогоне
        stats["extracted"] += 1

    archive.save_seen(seen_ids)
    logger.info("Сборщик: прогон завершён — %s", stats)
    return stats
