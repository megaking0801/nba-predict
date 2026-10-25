"""Static geography and date windows for the schedule/context features (P4.1).

All values are public facts typed in by hand; tests/test_context.py checks them against known
city-pair distances. Arena coordinates are good to roughly a kilometre, which is all the
features need. Intuit Dome (LAC since 2024-25) is about 13 km from the old shared arena, so
LAC keeps one location for every season; other pre-2019 arena moves (DET, GSW, SAC) are 8-40 km
and ignored.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Dict, Tuple

# abbr -> (latitude, longitude, IANA time zone, arena elevation in metres)
TEAM_VENUE: Dict[str, Tuple[float, float, str, float]] = {
    "ATL": (33.757, -84.396, "America/New_York", 320),
    "BKN": (40.683, -73.975, "America/New_York", 10),
    "BOS": (42.366, -71.062, "America/New_York", 6),
    "CHA": (35.225, -80.839, "America/New_York", 220),
    "CHI": (41.881, -87.674, "America/Chicago", 180),
    "CLE": (41.497, -81.688, "America/New_York", 200),
    "DAL": (32.791, -96.810, "America/Chicago", 130),
    "DEN": (39.749, -105.008, "America/Denver", 1609),
    "DET": (42.341, -83.055, "America/New_York", 180),
    "GSW": (37.768, -122.388, "America/Los_Angeles", 5),
    "HOU": (29.751, -95.362, "America/Chicago", 15),
    "IND": (39.764, -86.156, "America/Indiana/Indianapolis", 215),
    "LAC": (34.043, -118.267, "America/Los_Angeles", 90),
    "LAL": (34.043, -118.267, "America/Los_Angeles", 90),
    "MEM": (35.138, -90.051, "America/Chicago", 80),
    "MIA": (25.781, -80.187, "America/New_York", 2),
    "MIL": (43.045, -87.917, "America/Chicago", 190),
    "MIN": (44.980, -93.276, "America/Chicago", 250),
    "NOP": (29.949, -90.082, "America/Chicago", 1),
    "NYK": (40.751, -73.993, "America/New_York", 10),
    "OKC": (35.463, -97.515, "America/Chicago", 365),
    "ORL": (28.539, -81.384, "America/New_York", 30),
    "PHI": (39.901, -75.172, "America/New_York", 5),
    "PHX": (33.446, -112.071, "America/Phoenix", 330),
    "POR": (45.532, -122.667, "America/Los_Angeles", 15),
    "SAC": (38.580, -121.500, "America/Los_Angeles", 10),
    "SAS": (29.427, -98.438, "America/Chicago", 200),
    "TOR": (43.644, -79.379, "America/Toronto", 76),
    "UTA": (40.768, -111.901, "America/Denver", 1288),
    "WAS": (38.898, -77.021, "America/New_York", 5),
    # Not a team: the Raptors' temporary home (Amalie Arena, Tampa) during the 2020-21 border closure.
    "TPA": (27.943, -82.452, "America/New_York", 15),
}

# (home abbr, first day, last day) -> arena key used instead of the team's own. The Raptors played
# every 2020-21 home game in Tampa; the window is the whole 2020-21 season (the date of their
# return to Toronto during that season is unverified, so late-season games may be off).
VENUE_OVERRIDES = [("TOR", date(2020, 12, 22), date(2021, 7, 31), "TPA")]

BUBBLE_START, BUBBLE_END = date(2020, 7, 30), date(2020, 10, 12)
CROWD_RESTRICTED = (date(2020, 12, 22), date(2021, 5, 31))   # date approximation, no attendance data

_EARTH_RADIUS_KM = 6371.0088


def arena_key(home_abbr: str, d: date) -> str:
    """The TEAM_VENUE key of the arena where ``home_abbr`` hosts on ``d``."""
    for abbr, first, last, key in VENUE_OVERRIDES:
        if abbr == home_abbr and first <= d <= last:
            return key
    return home_abbr


def is_bubble(d: date) -> bool:
    return BUBBLE_START <= d <= BUBBLE_END


def is_crowd_restricted(d: date) -> bool:
    return CROWD_RESTRICTED[0] <= d <= CROWD_RESTRICTED[1]


def distance_km(a: str, b: str) -> float:
    """Great-circle distance between two teams' arenas."""
    lat1, lon1 = map(math.radians, TEAM_VENUE[a][:2])
    lat2, lon2 = map(math.radians, TEAM_VENUE[b][:2])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def utc_offset_hours(abbr: str, d: date) -> float:
    """UTC offset of the team's arena at noon local time on ``d`` (so daylight saving is right)."""
    tz = ZoneInfo(TEAM_VENUE[abbr][2])
    return datetime(d.year, d.month, d.day, 12, tzinfo=tz).utcoffset().total_seconds() / 3600.0


def tz_shift_hours(from_abbr: str, to_abbr: str, d: date) -> float:
    """Clock shift of travelling from one arena to another: positive = east."""
    return utc_offset_hours(to_abbr, d) - utc_offset_hours(from_abbr, d)


def elevation_m(abbr: str) -> float:
    return TEAM_VENUE[abbr][3]
