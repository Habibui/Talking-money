"""
Время по Москве — общее для main.py: определяет, наступил ли момент "флаша"
накопленной очереди рутинных новостей одним дайджестом.
"""

from datetime import datetime, timedelta, timezone

# Москва — фиксированный UTC+3 круглый год (без перевода часов с 2014 года),
# поэтому для наших целей достаточно простого смещения без zoneinfo/tzdata.
_MSK_OFFSET = timedelta(hours=3)


def _now_msk(now_utc: datetime | None = None) -> datetime:
    return (now_utc or datetime.now(timezone.utc)) + _MSK_OFFSET


def hour_msk(now_utc: datetime | None = None) -> int:
    return _now_msk(now_utc).hour


def flush_key_msk(now_utc: datetime | None = None) -> str:
    """Уникальный ключ текущего часа по МСК вида "2026-09-09-12" — используется,
    чтобы отправить дайджест ровно один раз за этот час, даже если в тот же час
    (например, 12:00-12:59) пайплайн успеет отработать несколько раз подряд
    (запуск каждые 15 минут)."""
    return _now_msk(now_utc).strftime("%Y-%m-%d-%H")


def is_night_msk(now_utc: datetime | None = None) -> bool:
    """23:00–08:00 МСК. Используется НЕ для того, чтобы задерживать публикацию
    (breaking по-прежнему публикуется сразу, в любое время суток — иначе теряет
    смысл формата "срочное"), а только для того, чтобы ночью отправлять посты
    в Telegram без звука/вибрации (см. telegram_bot.send_message(silent=...)) —
    так подписчиков не будит уведомление, но при этом никто не ждёт до утра,
    если сам открыл канал."""
    hour = hour_msk(now_utc)
    return hour >= 23 or hour < 8
