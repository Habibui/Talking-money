"""
Экстрактор (v2, роль 1) — извлечение структурированной заметки из одной
статьи, без перевода и без авторского тона (это делает позже Аналитик).
Промпт закреплён после двух раундов ревью менеджера проекта — см.
claude/format-v2-prompts-draft.md (третья версия, раздел 1) в Cowork
Project. Конвенции вызова (клиент/парсинг JSON/обработка ошибок) — те же,
что в src/llm.py (v1): контракт "None при неустранимой ошибке", терпимый
JSON-парсинг через raw_decode, отдельные классы ошибок формата vs сети.
"""

import json
import logging
from datetime import datetime, timezone

import anthropic

from . import config

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
Ты — модуль извлечения фактов для экономического Telegram-канала
«О чём talk'уют деньги». Тебе на вход дают одну статью. Твоя задача —
извлечь структурированную заметку, а НЕ написать пост для канала: без
иронии, без личного комментария, без авторского тона — чистое изложение
фактов.

Верни JSON:
{
  "topics": [...],       // один или несколько из: markets, oil_gas, energy,
                          // macro, geopolitics_econ, other
  "region": "...",       // страна/регион, к которому относится новость
  "actors": [...],       // организации/компании/персоны/регуляторы
  "key_facts": [...],    // 2-4 предложения на ЯЗЫКЕ ИСТОЧНИКА, только факты
  "importance": 1-5,
  "importance_reason": "...",   // одна строка обоснования
  "market_link": "..." | null,
  "content_level": "full" | "lead" | "headline",
  "tags_en": [...]       // 3-8 тегов СТРОГО из списка ниже, lower case
}

Правила:
1. key_facts — на ЯЗЫКЕ ИСТОЧНИКА, НЕ переводи. Для английских источников
   (CNBC, Bloomberg, NYT, BBC, WSJ, MarketWatch, Investing.com) — пиши
   key_facts на английском. Для русскоязычных источников (ЦБ РФ,
   Ведомости) — на русском. Перевод на русский происходит позже, на этапе
   Аналитика — не делай его здесь: ранний перевод искажает именно то, с
   чем впоследствии сверяется Фактчекер. Цифры, суммы, проценты — дословно
   из источника на языке источника, никогда не заменяй их словами вроде
   "существенно"/"значительно"/"резко". Если точного значения в источнике
   нет — не выдумывай его.
2. Если и полный текст, и лид пустые (источник не отдал контент) —
   key_facts должны содержать только то, что прямо следует из заголовка, и
   ни одной правдоподобной, но не подтверждённой детали.
3. content_level — честно отражает, что реально было на входе, а не то,
   насколько подробной вышла заметка: "full", если был полный текст через
   trafilatura; "lead", если только лид/сниппет из RSS без полного текста;
   "headline", если и лид пустой и есть только заголовок. Это не оценка
   качества заметки, а фиксация глубины источника — дальше по пайплайну
   Отборщик и Аналитик используют это поле, чтобы не строить сюжет и не
   брать точные цифры из заметок, где под текстом нет реальной опоры.
4. importance — калибровочные якоря (не жёсткие правила):
   5 — решение ФРС/ЕЦБ/Банка России по ставке, скачок/обвал нефти >5%,
       крупный суверенный дефолт, санкционный пакет с прямым эффектом на
       энергетику/финансы.
   4 — отчётность мегакапа с сильным отклонением от прогноза, решение
       крупного ЦБ вне "большой тройки" по ставке, сделка/банкротство с
       системным эффектом.
   3 — плановая макростатистика в рамках/чуть вне ожиданий, отчётность
       крупной компании в рамках прогноза, региональный торговый спор с
       прямым экономическим следствием.
   2 — рутинная отчётность без сюрпризов, второстепенные назначения в
       регуляторах, локальные корпоративные новости с ограниченным
       рыночным эффектом.
   1 — решение небольшого/периферийного ЦБ без сюрприза, техническая
       новость без прямого рыночного эффекта.
5. market_link — коротко, через что событие может повлиять на
   рынки/курсы/сырьё; если эффект неочевиден — null, не притягивай связь.
6. tags_en — выбирай ТОЛЬКО из этого списка (не изобретай новый вариант
   уже существующего понятия — иначе поиск по архиву перестанет находить
   связанные заметки):

   fed, ecb, cbr, pboc, boj, boe, opec, oil, gas, energy, commodities,
   gold, metals, agri, sanctions, tariffs, trade, china, us, eu, russia,
   uk, japan, india, middle_east, inflation, rates, gdp, jobs, budget,
   housing, bonds, fx, ruble, dollar, euro, yuan, equities, earnings,
   banks, tech, ai, crypto, shipping, default, imf, wto, geopolitics_econ

   Новый тег вне этого списка — только если реально ни один из них не
   подходит (редкий случай, не рутина). Формат: lowercase_snake,
   единственное число (banks, а не bank/banking). Список — стартовый по
   состоянию на 24.09.2026, дополнен 26.09.2026 (budget, housing, metals,
   agri, crypto, india, middle_east); через неделю работы на реальных
   данных будет построена таблица синонимов (alias → канонический тег) без
   повторного прогона уже сохранённых заметок — сейчас просто строго
   придерживайся списка."""

_REQUIRED_KEYS = {
    "topics", "region", "actors", "key_facts", "importance",
    "importance_reason", "market_link", "content_level", "tags_en",
}

_VALID_CONTENT_LEVELS = {"full", "lead", "headline"}


def _content_level_hint(article_text: str, summary: str) -> str:
    """Объективная (не модельная) оценка глубины входного текста — считаем
    её ground truth и используем как страховку от ошибки модели в
    content_level (см. override ниже): в отличие от остальных полей
    JSON-ответа, content_level — не суждение, а факт о том, что было на
    входе, а это Сборщик знает точно, независимо от модели."""
    if article_text and len(article_text.strip()) > 200:
        return "full"
    if summary and summary.strip():
        return "lead"
    return "headline"


def extract_note(item: dict) -> dict | None:
    """item — {source, title, link, language, article_text, summary}.
    article_text — полный текст через trafilatura (sources.fetch_article_lead),
    если удалось получить, иначе "". summary — лид/сниппет из RSS/телеграма
    (может тоже быть пустым — см. sources.py). Возвращает dict с полями
    Экстрактора + content_level (проверенный/подставленный программно, см.
    _content_level_hint), либо None при неустранимой ошибке (сеть/формат
    ответа после повтора) — вызывающий код (Сборщик) должен просто
    пропустить эту статью в этом прогоне, не блокируя остальные."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=30.0)

    hint = _content_level_hint(item.get("article_text", ""), item.get("summary", ""))
    body_text = item.get("article_text") or item.get("summary") or ""
    if not body_text.strip():
        body_field = (
            "[источник не отдал ни полный текст, ни лид — единственный "
            "источник фактов здесь заголовок ниже; не добавляй ни одной "
            "детали, которой в заголовке нет]"
        )
    else:
        body_field = body_text[: config.MAX_ARTICLE_CHARS]

    user_content = (
        f"Источник: {item['source']}\n"
        f"Язык источника: {item.get('language', 'en')}\n"
        f"Заголовок: {item['title']}\n"
        f"Текст: {body_field}\n"
    )

    try:
        response = client.messages.create(
            model=config.MODEL_EXTRACTOR,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()

        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            if raw.lower().startswith("json"):
                raw = raw[4:]

        data, _ = json.JSONDecoder().raw_decode(raw)
        missing = _REQUIRED_KEYS - set(data.keys())
        if missing:
            raise ValueError(f"В ответе Экстрактора нет ключей: {missing}")

        # content_level — не оставляем на доверии модели: это факт о входе,
        # а не суждение, поэтому при несовпадении с hint подставляем hint и
        # только предупреждаем в лог (не считаем сбоем всего вызова —
        # остальные поля отдельной проверки не требуют).
        if data.get("content_level") not in _VALID_CONTENT_LEVELS or data["content_level"] != hint:
            logger.warning(
                "content_level модели (%r) не совпал с объективной оценкой "
                "входа (%r) для %s (%s) — используем объективную оценку",
                data.get("content_level"), hint, item["source"], item["link"],
            )
            data["content_level"] = hint

        return data

    except (json.JSONDecodeError, ValueError) as exc:
        logger.error(
            "Сбой формата ответа Экстрактора для %s (%s): %s",
            item["source"], item["link"], exc,
        )
        return None
    except Exception as exc:
        # сетевые/API-ошибки — повторы уже делает сам anthropic SDK, сдаёмся
        logger.error(
            "Ошибка вызова Экстрактора для %s (%s): %s", item["source"], item["link"], exc
        )
        return None
