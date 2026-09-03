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
]

# --- Прочее -------------------------------------------------------------------

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "state", "posted.json")
MAX_STATE_IDS = 3000  # сколько последних id хранить в state, чтобы файл не рос бесконечно
MAX_ITEMS_PER_SOURCE_PER_RUN = 10  # защита от аномального всплеска (сломанный фид и т.п.)
REQUEST_TIMEOUT = 20  # секунд, для http-запросов
