"""
Определение "ночного" времени по Москве — общее для main.py (решает, копить
новость в ночную очередь или публиковать сразу).
"""

from datetime import datetime, timedelta, timezone

from . import config

# Москва — фиксированный UTC+3 круглый год (без перевода часов с 2014 года),
# поэтому для наших целей достаточно простого смещения без zoneinfo/tzdata.
_MSK_OFFSET = timedelta(hours=3)


def is_night_msk(now_utc: datetime | None = None) -> bool:
    """Ночное окно (по умолчанию 23:00–08:00 МСК, см. config). В это время
    новости не публикуются по одной сразу, а копятся в очередь и уходят одним
    дайджестом первым дневным запуском после конца окна."""
    now_msk = (now_utc or datetime.now(timezone.utc)) + _MSK_OFFSET
    hour = now_msk.hour
    start, end = config.NIGHT_DIGEST_START_HOUR_MSK, config.NIGHT_DIGEST_END_HOUR_MSK
    return hour >= start or hour < end
