"""
Конфигурация пайплайна.

Секреты (токены/ключи) сюда НЕ кладём — они приходят из переменных окружения,
которые GitHub Actions подставляет из репозиторных секретов (см. README.md).
"""

import os

# --- Секреты и параметры окружения -----------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")  # например "@talkuyut_dengi" или числовой id канала
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# --- Модель для перевода + комментария --------------------------------------

# Claude Haiku 4.5 — дешёвая и быстрая модель, достаточная для перевода
# заголовка + короткого комментария. При желании поднять качество — заменить
# на claude-sonnet-4-5 (дороже, но не критично при таком объёме).
LLM_MODEL = "claude-haiku-4-5-20251001"

# --- Источники ---------------------------------------------------------------
# type: "rss" — обычный RSS/Atom-фид, читаем через feedparser
#       "telegram" — публичный телеграм-канал, читаем через t.me/s/<name>
#       (не требует токена/логина — используется публичная HTML-версия
#       предпросмотра канала)

SOURCES = [
    {
        "name": "CNBC",
        "type": "rss",
        # Economy — уже, чем общий Business (меньше спорта-как-бизнеса и лайфстайла)
        "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    },
    {
        "name": "NYT",
        "type": "rss",
        "url": "https://www.nytimes.com/svc/collections/v1/publish/https://www.nytimes.com/section/business/rss.xml",
    },
    {
        "name": "BBC",
        "type": "rss",
        "url": "https://feeds.bbci.co.uk/news/business/rss.xml",
    },
    {
        "name": "WSJ",
        "type": "rss",
        # Markets — курсы, облигации, сырьё, реакции рынков на решения ЦБ/политиков
        "url": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
    },
    {
        "name": "MarketWatch",
        "type": "rss",
        "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    },
    {
        "name": "Bloomberg",
        "type": "telegram",
        # У Bloomberg нет публичного RSS — берём из их официального
        # телеграм-канала через публичную веб-версию превью.
        "telegram_channel": "bloomberg",
    },
    {
        "name": "Investing.com",
        "type": "rss",
        # Economy News — проверено вручную: заголовки прямо по нашему углу
        # (решения ФРС, долговой рынок США, нефть/геополитика на Ближнем
        # Востоке, макростатистика Китая), не общий "все новости подряд".
        "url": "https://www.investing.com/rss/news_14.rss",
    },
]

# --- Ночной дайджест ----------------------------------------------------------
# Пайплайн продолжает забирать и переводить новости круглосуточно (сознательное
# решение по каденции не менять), но с 23:00 до 08:00 МСК они не публикуются
# по одной сразу, а копятся в очередь (state/night_queue.json) и уходят одним
# сборным постом первым же запуском после конца окна. Причина: часть
# подписчиков — реальные знакомые, добавленные вручную, и поток отдельных
# постов посреди ночи их будит; плюс дайджест читается лучше, чем россыпь
# сообщений. Окно ниже — момент, откуда его при необходимости менять (например,
# если аудитория станет заметно нероссийской — см. обоснование в чате).
NIGHT_DIGEST_START_HOUR_MSK = 23  # с 23:00 МСК копим в очередь
NIGHT_DIGEST_END_HOUR_MSK = 8     # до 08:00 МСК; после — дайджест и снова обычные посты

# --- Прочее -------------------------------------------------------------------

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "state", "posted.json")
NIGHT_QUEUE_PATH = os.path.join(os.path.dirname(__file__), "..", "state", "night_queue.json")
MAX_STATE_IDS = 3000  # сколько последних id хранить в state, чтобы файл не рос бесконечно
MAX_ITEMS_PER_SOURCE_PER_RUN = 10  # защита от аномального всплеска (сломанный фид и т.п.)
REQUEST_TIMEOUT = 20  # секунд, для http-запросов
MAX_ARTICLE_CHARS = 6000  # ограничение на длину текста статьи, который отдаём Claude
