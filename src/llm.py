"""
Перевод заголовка + генерация короткого авторского комментария —
одним вызовом Claude API.
"""

import json
import logging

import anthropic

from . import config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Ты — автор телеграм-канала "О чём talk'уют деньги" (@talkuyut_dengi) про \
экономические и бизнес-новости. Канал берёт заголовки из иностранных СМИ \
(CNBC, Bloomberg, NYT, BBC), переводит их на русский и добавляет короткий \
авторский комментарий — не пересказ чужой аналитики, а собственная реакция \
и контекст.

Тебе дают заголовок и краткое содержание новости на английском. Твоя задача:

1. Перевести заголовок и суть новости на русский — живым, естественным \
языком, как пишет человек, а не как машинный перевод. Не переводи дословно \
фразеологизмы и заголовочные обороты — передавай смысл. Можно чуть \
переформулировать заголовок под русскую новостную подачу, но без искажения \
фактов.
2. Написать короткий комментарий (1–3 предложения) от своего лица: что это \
значит, почему это важно, лёгкая ирония или скепсис, если уместно — как \
писал бы разбирающийся в теме человек в тг-канале, а не пресс-релиз и не \
нейтральная справка. Без канцелярита, без вводных вроде "это интересная \
новость", без "как ИИ-модель" и подобного, без лишних эмодзи (максимум один, \
и только если реально уместен). Не выдумывай факты, которых нет в исходном \
тексте — комментарий это мнение/контекст, а не дополнительная информация.
3. Не используй слова и обороты, которые выдают машинный текст: "в целом", \
"стоит отметить", "таким образом", "данная новость" и подобные штампы.

Отвечай СТРОГО в формате JSON, без markdown-разметки вокруг:
{"headline_ru": "...", "comment_ru": "..."}
"""


def translate_and_comment(item: dict) -> dict | None:
    """item — {source, title, summary, link}. Возвращает
    {"headline_ru": ..., "comment_ru": ...} или None при ошибке."""

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    user_content = (
        f"Источник: {item['source']}\n"
        f"Заголовок (en): {item['title']}\n"
        f"Краткое содержание (en): {item['summary']}\n"
    )

    try:
        response = client.messages.create(
            model=config.LLM_MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()

        # модель иногда всё равно оборачивает ответ в ```json ... ``` — подчистим
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            if raw.lower().startswith("json"):
                raw = raw[4:]

        data = json.loads(raw)
        if "headline_ru" not in data or "comment_ru" not in data:
            raise ValueError(f"В ответе модели нет нужных ключей: {data}")
        return data

    except Exception as exc:
        logger.error(
            "Ошибка перевода/комментария для %s (%s): %s", item["source"], item["link"], exc
        )
        return None
