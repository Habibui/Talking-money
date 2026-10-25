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

import html as _html
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
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


TELEGRAM_TEXT_LIMIT = 4096


def split_message(text: str, limit: int = 3900) -> list[str]:
    """11.10.2026 — страховка от отказа Telegram (400, текст длиннее 4096
    знаков) для готового сообщения. Длину считаем по тексту БЕЗ разметки —
    Telegram считает именно его. Если уложились — возвращаем как есть, одним
    элементом (обычный случай: ежедневный выпуск целится в 3500, недельный в
    3800). Если нет — режем по границам абзацев ("\n\n"): блок выпуска —
    это один абзац-группа с собственными тегами, так что разметка в частях
    остаётся целой. Абзац, который сам длиннее лимита, остаётся целым (его
    не режем посреди тега) — это крайний случай, отправка тогда упадёт, как
    и раньше, а ошибка попадёт в лог."""
    def plain_len(t: str) -> int:
        return len(_TAG_RE.sub("", t))

    if plain_len(text) <= TELEGRAM_TEXT_LIMIT:
        return [text]
    parts, current = [], ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if current and plain_len(candidate) > limit:
            parts.append(current)
            current = para
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


# 03.10.2026 — в живом выпуске в поле "link" утекло «(блок 2)»: модель сослалась
# на соседний блок по номеру, а читатель никаких номеров не видит. Правило 12
# Аналитика теперь это запрещает, а эта страховка вырезает такую отсылку
# детерминированно, если модель всё-таки её написала (в скобках, любой падеж).
_BLOCK_REF_RE = re.compile(r"\s*\(\s*блок[а-я]*\s*\d+\s*\)", re.IGNORECASE)


def strip_block_refs(text: str) -> str:
    return _BLOCK_REF_RE.sub("", text)


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
            parts.append(strip_block_refs(block["link"]))
        parts.append("")

    # 30.09.2026 — "Что это значит для нас" (п.2, решение автора):
    # необязательное поле, см. analyst.py SYSTEM_PROMPT правило 15 — модель
    # сама решает, писать ли его (null, если для этого выпуска нет реального
    # сюжета с прямым следствием для России), рендер только показывает то,
    # что пришло, не подставляет ничего своего.
    for_us = issue.get("for_us")
    if for_us:
        parts.append(f"<b>Что это значит для нас:</b> {for_us}")
        parts.append("")

    watch_next = issue.get("watch_next")
    if watch_next:
        parts.append(f"<b>Что смотреть дальше:</b> {watch_next}")

    return "\n".join(parts).strip()


# --- Недельный выпуск: отдельное оформление (11.10.2026, решение автора) ------
# Цель — чтобы «Аналитика недели» с первого взгляда отличалась от обычных
# постов и дайджестов канала и легко читалась: заголовок-плашка, эмодзи темы
# у каждого блока, разделители, подписанные секции. Как и в дайджесте
# (telegram_bot._DIGEST_TOPIC_EMOJI), эмодзи выбирает КОД, а не модель:
# модель возвращает только текстовый код темы `topic` у блока, символ по нему
# подставляется здесь — одна опечатка модели не протащит в канал случайный
# значок. Набор тем — те же шесть, что у дайджеста, плюс две для аналитики.
WEEKLY_TOPIC_EMOJI = {
    "oil_gas": "🛢",
    "energy": "⚡",
    "markets": "📈",
    "central_banks": "🏦",
    "geopolitics": "🌍",
    "russia": "🇷🇺",
    "currency": "💵",
    "prices": "🛒",
}
WEEKLY_DEFAULT_EMOJI = "📌"
WEEKLY_LABEL = "📊 Аналитика недели"
WEEKLY_DIVIDER = "▬▬▬▬▬▬▬▬▬▬"
# 25.10.2026 — один хештег вместо двух: метка «📊 Аналитика недели» уже
# отличает пост, а по хештегу ленту аналитики можно открыть одним нажатием.
WEEKLY_HASHTAGS = "#аналитика"
# Telegram считает до 4096 знаков текста без разметки; берём с запасом.
WEEKLY_SAFE_LIMIT = 4000


# 25.10.2026 — ссылки на источники в недельном выпуске. Аналитик пишет
# в тексте метку [[Издание|source_id]] (URL модель не пишет — только id
# заметки), а настоящую ссылку подставляет код из заметки. Метка без
# известного id или с небезопасным URL превращается в простое название.
SOURCE_MARKER_RE = re.compile(r"\[\[([^\[\]|]{1,60})\|([0-9A-Za-z_\-]{4,64})\]\]")
_TRACKING_PARAM_PREFIXES = ("utm_", "at_medium", "at_campaign", "at_")


def clean_source_url(url: str | None) -> str | None:
    """http(s)-ссылка без трекинговых параметров RSS (at_medium=RSS и т.п.)
    или None, если ссылка пустая или небезопасная."""
    if not url or not isinstance(url, str):
        return None
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith(_TRACKING_PARAM_PREFIXES)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def linkify_sources(text: str, links: dict | None = None) -> str:
    """Метки [[Издание|id]] -> <a href="...">Издание</a> (если ссылка для id
    известна и безопасна) или просто «Издание». links — {id: url}."""
    def repl(m):
        name = _html.escape(m.group(1).strip(), quote=False)
        url = clean_source_url((links or {}).get(m.group(2)))
        if not url:
            return name
        return f'<a href="{_html.escape(url, quote=True)}">{name}</a>'
    return SOURCE_MARKER_RE.sub(repl, text or "")


def strip_source_markers(text: str) -> str:
    """Метки -> только название издания (для hook, заголовков и памяти)."""
    return SOURCE_MARKER_RE.sub(lambda m: m.group(1).strip(), text or "")


def plain_length(html: str) -> int:
    """Длина текста без разметки — так её считает Telegram для лимита 4096."""
    return len(_TAG_RE.sub("", html))


def restore_block_topics(original: dict, corrected: dict) -> dict:
    """Фактчекер возвращает corrected_draft своими словами и может потерять
    служебное поле `topic` у блоков (оно не часть исходной схемы). Если число
    блоков не изменилось — переносим `topic` из черновика Аналитика по
    индексу; если изменилось — блоки без `topic` получат эмодзи по
    умолчанию (это только значок, не факт). Возвращает corrected."""
    orig_blocks = original.get("blocks") or []
    new_blocks = corrected.get("blocks") or []
    if len(orig_blocks) == len(new_blocks):
        for ob, nb in zip(orig_blocks, new_blocks):
            if not nb.get("topic") and ob.get("topic"):
                nb["topic"] = ob["topic"]
    return corrected


def _weekly_block_html(block: dict, links: dict | None = None) -> str:
    emoji = WEEKLY_TOPIC_EMOJI.get(block.get("topic"), WEEKLY_DEFAULT_EMOJI)
    title = strip_source_markers(block["title"]).strip()
    if "<b>" not in title:  # модель обязана оборачивать сама (правило 13), но не полагаемся
        title = f"<b>{title}</b>"
    lines = [f"{emoji} {title}", linkify_sources(block["what"], links)]
    if block.get("meaning"):
        lines.append(f"👉 {linkify_sources(block['meaning'], links)}")
    # поле link (связка с другим блоком) в недельном выпуске не выводится: отдельной
    # строкой под 👉 она читалась как потерянный хвост (выпуск 18.10.2026)
    return "\n".join(lines)


def _weekly_html(issue: dict, blocks: list[dict], period: str, links: dict | None = None) -> str:
    # Первая строка — hook (едкий, в голосе канала: его видно в уведомлении),
    # плашка с периодом — второй строкой, как метка, а не заголовок.
    parts = [
        f"🔥 <b>{strip_source_markers(issue['hook'])}</b>",
        f"<i>{WEEKLY_LABEL} · {period}</i>",
        WEEKLY_DIVIDER,
        "",
    ]
    for block in blocks:
        parts.extend([_weekly_block_html(block, links), ""])
    parts.append(WEEKLY_DIVIDER)
    parts.append("")
    if issue.get("for_us"):
        parts.extend([f"🎯 <b>Что это значит для нас</b>\n{linkify_sources(issue['for_us'], links)}", ""])
    parts.append(WEEKLY_HASHTAGS)
    return "\n".join(parts).strip()


def render_weekly_html(
    issue: dict, period: str, limit: int | None = WEEKLY_SAFE_LIMIT, links: dict | None = None,
) -> tuple[str, list[str]]:
    """Недельный выпуск. period — «5–11 октября».

    limit — потолок длины (знаков текста без разметки): если текст не
    влезает, из выпуска целиком убирается последний блок (блоки идут по
    убыванию важности — так их отдаёт Отборщик), и так пока не влезет. Блок,
    на который опирается hook (`hook_block_refs`), не убирается — hook остался
    бы без опоры. limit=None — без ограничения (вариант 2 тестового режима,
    вызывающий код режет результат на сообщения сам). Возвращает (html,
    список заголовков убранных блоков) — вызывающий код пишет их в лог и
    сообщает автору. Раздела «что смотреть дальше» в недельном выпуске нет
    (решение автора 11.10.2026), поле watch_next игнорируется."""
    blocks = list(issue.get("blocks") or [])
    protected = set(issue.get("hook_block_refs") or [])
    # 18.10.2026 — российский блок тоже не убирается: раздел «для нас» опирается
    # на него (в выпуске 18.10 обрезка сняла его, и вывод повис без базы)
    protected |= {i for i, b in enumerate(blocks) if b.get("topic") == "russia"}
    dropped: list[str] = []
    html = _weekly_html(issue, blocks, period, links)
    while limit is not None and len(_TAG_RE.sub("", html)) > limit and len(blocks) > 1:
        idx = next((i for i in range(len(blocks) - 1, -1, -1) if i not in protected), None)
        if idx is None:
            break
        dropped.append(_TAG_RE.sub("", blocks[idx].get("title", "")))
        del blocks[idx]
        protected = {i if i < idx else i - 1 for i in protected}
        html = _weekly_html(issue, blocks, period, links)
    return html, dropped
