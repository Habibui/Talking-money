"""
Рендер выпуска v2 в текст для Telegram (HTML-разметка) + валидация этой
разметки ПЕРЕД отправкой. Второе — прямой ответ на пункт 3 второго раунда
ревью менеджера (см. format-v2-prompts-draft.md, «Второй раунд ревью»):
«нет проверки валидности Telegram HTML перед отправкой, особенно после
corrected_draft; при ошибке — отправка без разметки + уведомление автору, а
не тихий сбой». validate_telegram_html() ниже — эта проверка; вызывающий
код (main-скрипт v2, когда он будет отправлять в Telegram, а не только
DRY_RUN) должен следовать контракту: если validate_telegram_html(text) is
False — послать strip_telegram_html(text) (plain text) вместо text, и
отдельно уведомить автора личным сообщением, а не просто проглотить
ошибку отправки Telegram API.

Telegram Bot API поддерживает не весь HTML, а конкретный список тегов
(https://core.telegram.org/bots/api#html-style) — в частности, НЕТ
авто-закрытия тегов и вложенность должна быть строгой (в отличие от
браузерного HTML, где парсер многое исправляет сам). Валидатор ниже — не
полный HTML-парсер, а специально узкая проверка именно под это: набор
разрешённых тегов, у <a> обязателен href, и все теги должны быть закрыты
в правильном порядке (строгий стек, без "теги пересекаются").
"""

import logging
import re
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

# https://core.telegram.org/bots/api#html-style — теги, которые Telegram
# реально поддерживает в parse_mode=HTML. tg-spoiler/tg-emoji — тоже
# валидны у Telegram, но канал их не использует; включены на будущее, не
# как мёртвый код (если понадобятся — валидатор их уже пропустит).
_ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "span", "tg-spoiler", "a", "code", "pre", "blockquote", "tg-emoji",
}
_REQUIRES_HREF = {"a"}


class _TelegramHTMLValidator(HTMLParser):
    """Копит найденные проблемы в self.errors, не бросает исключение сразу —
    так одним проходом можно получить все проблемы для лога/уведомления
    автору, а не только первую."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            self.errors.append(f"неподдерживаемый Telegram тег <{tag}>")
            return
        if tag in _REQUIRES_HREF and not any(name == "href" for name, _ in attrs):
            self.errors.append(f"<{tag}> без href")
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        # самозакрывающихся тегов вида <br/> в разрешённом списке нет —
        # само появление здесь для любого тега из _ALLOWED_TAGS означает,
        # что его открыли и сразу "закрыли" без пары <tag>...</tag>, что
        # Telegram не поддерживает никогда (в отличие от обычного HTML)
        self.errors.append(f"самозакрывающийся тег <{tag}/> не поддерживается Telegram")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if not self.stack:
            self.errors.append(f"</{tag}> без открывающего тега")
            return
        if self.stack[-1] != tag:
            self.errors.append(
                f"</{tag}> не совпадает с последним открытым тегом <{self.stack[-1]}> "
                f"(теги должны закрываться строго в обратном порядке открытия)"
            )
            return
        self.stack.pop()

    def error(self, message):  # HTMLParser (py<3.10) совместимость
        self.errors.append(str(message))


def validate_telegram_html(text: str) -> tuple[bool, list[str]]:
    """Возвращает (валидно, список_проблем). Валидно = список тегов только
    из разрешённых Telegram, у всех <a> есть href, все теги закрыты в
    правильном порядке (стек пуст в конце). Не проверяет длину сообщения
    (лимиты Telegram на длину — отдельная забота вызывающего кода, не эта
    функция) и не проверяет валидность самого href как URL — это уже не
    вопрос HTML-разметки."""
    validator = _TelegramHTMLValidator()
    try:
        validator.feed(text)
        validator.close()
    except Exception as exc:  # HTMLParser в редких случаях может исключение бросить
        return False, [f"ошибка разбора HTML: {exc}"]

    errors = list(validator.errors)
    if validator.stack:
        errors.append(f"незакрытые теги: {', '.join(validator.stack)}")
    return (len(errors) == 0), errors


_TAG_RE = re.compile(r"<[^>]+>")


def strip_telegram_html(text: str) -> str:
    """Запасной вариант отправки, когда validate_telegram_html() вернула
    False (см. docstring модуля) — грубо снимает всю разметку, чтобы хотя
    бы сам текст ушёл читателю, а не потерялся из-за ошибки в HTML."""
    return _TAG_RE.sub("", text)


def render_issue_html(issue: dict) -> str:
    """issue — черновик выпуска (после Фактчекера — обычно
    factcheck["corrected_draft"], но функция принимает любой dict в этом
    формате: hook/blocks/watch_next; hook_block_refs/watch_next_source_ids/
    source_ids игнорируются здесь — они нужны были только Фактчекеру для
    проверки, в текст поста не идут)."""
    parts = [f"<b>{issue['hook']}</b>", ""]

    for block in issue.get("blocks", []):
        # 26.09.2026 — НЕ оборачиваем в <b> здесь: промпт Аналитика
        # (analyst.py, SYSTEM_PROMPT, правило 13) уже прямо требует, чтобы
        # модель сама оборачивала заголовок блока в <b>...</b> — первый
        # реальный полный прогон показал результат такого дублирования:
        # <b><b>...</b></b> в готовом выпуске (syntactически валидно для
        # Telegram, но лишняя вложенность — не то, что задумано). hook и
        # watch_next модель НЕ оборачивает сама (в промпте это не
        # требуется только для title), поэтому их оборачивание здесь ниже
        # остаётся как было — не трогаем то, что не сломано.
        parts.append(block["title"])
        parts.append(block["what"])
        if block.get("meaning"):
            parts.append(block["meaning"])
        if block.get("link"):
            parts.append(block["link"])
        parts.append("")

    watch_next = issue.get("watch_next")
    if watch_next:
        parts.append(f"<b>Что смотреть дальше:</b> {watch_next}")

    return "\n".join(parts).strip()
