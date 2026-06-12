"""Timezone helpers for the rebuilt pipeline. DB stores UTC; the NBA "game
day" is the US/Eastern date; Taipei exists only in the app display layer."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def et_today() -> dt.date:
    return dt.datetime.now(ET).date()


def et_date_of(ts_utc: dt.datetime) -> dt.date:
    """ET game-day of a UTC timestamp."""
    if ts_utc.tzinfo is None:
        ts_utc = ts_utc.replace(tzinfo=UTC)
    return ts_utc.astimezone(ET).date()
