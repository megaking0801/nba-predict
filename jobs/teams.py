"""Canonical NBA team data — the single source of truth for team identity.

Every ingest boundary must normalize to the canonical tricode here.
The old system's worst bug (GS/NO/NY/SA/UTAH/WSH never normalized, zeroing
features for 6 teams) is prevented by `require_abbr()` which raises on
anything unknown instead of silently defaulting.
"""
from __future__ import annotations

from typing import Dict, List

# abbr: (nba_team_id, full_name, espn_abbr, conference, name_ch)
TEAMS: Dict[str, tuple] = {
    "ATL": (1610612737, "Atlanta Hawks", "ATL", "East", "老鷹"),
    "BOS": (1610612738, "Boston Celtics", "BOS", "East", "塞爾提克"),
    "BKN": (1610612751, "Brooklyn Nets", "BKN", "East", "籃網"),
    "CHA": (1610612766, "Charlotte Hornets", "CHA", "East", "黃蜂"),
    "CHI": (1610612741, "Chicago Bulls", "CHI", "East", "公牛"),
    "CLE": (1610612739, "Cleveland Cavaliers", "CLE", "East", "騎士"),
    "DAL": (1610612742, "Dallas Mavericks", "DAL", "West", "獨行俠"),
    "DEN": (1610612743, "Denver Nuggets", "DEN", "West", "金塊"),
    "DET": (1610612765, "Detroit Pistons", "DET", "East", "活塞"),
    "GSW": (1610612744, "Golden State Warriors", "GS", "West", "勇士"),
    "HOU": (1610612745, "Houston Rockets", "HOU", "West", "火箭"),
    "IND": (1610612754, "Indiana Pacers", "IND", "East", "溜馬"),
    "LAC": (1610612746, "LA Clippers", "LAC", "West", "快艇"),
    "LAL": (1610612747, "Los Angeles Lakers", "LAL", "West", "湖人"),
    "MEM": (1610612763, "Memphis Grizzlies", "MEM", "West", "灰熊"),
    "MIA": (1610612748, "Miami Heat", "MIA", "East", "熱火"),
    "MIL": (1610612749, "Milwaukee Bucks", "MIL", "East", "公鹿"),
    "MIN": (1610612750, "Minnesota Timberwolves", "MIN", "West", "灰狼"),
    "NOP": (1610612740, "New Orleans Pelicans", "NO", "West", "鵜鶘"),
    "NYK": (1610612752, "New York Knicks", "NY", "East", "尼克"),
    "OKC": (1610612760, "Oklahoma City Thunder", "OKC", "West", "雷霆"),
    "ORL": (1610612753, "Orlando Magic", "ORL", "East", "魔術"),
    "PHI": (1610612755, "Philadelphia 76ers", "PHI", "East", "76人"),
    "PHX": (1610612756, "Phoenix Suns", "PHX", "West", "太陽"),
    "POR": (1610612757, "Portland Trail Blazers", "POR", "West", "拓荒者"),
    "SAC": (1610612758, "Sacramento Kings", "SAC", "West", "國王"),
    "SAS": (1610612759, "San Antonio Spurs", "SA", "West", "馬刺"),
    "TOR": (1610612761, "Toronto Raptors", "TOR", "East", "暴龍"),
    "UTA": (1610612762, "Utah Jazz", "UTAH", "West", "爵士"),
    "WAS": (1610612764, "Washington Wizards", "WSH", "East", "巫師"),
}

CANONICAL_ABBRS = frozenset(TEAMS.keys())

ABBR_TO_TEAM_ID: Dict[str, int] = {a: t[0] for a, t in TEAMS.items()}
TEAM_ID_TO_ABBR: Dict[int, str] = {t[0]: a for a, t in TEAMS.items()}
ABBR_TO_FULL_NAME: Dict[str, str] = {a: t[1] for a, t in TEAMS.items()}
TEAM_NAME_CH: Dict[str, str] = {a: t[4] for a, t in TEAMS.items()}

# ESPN variant -> canonical (covers both directions: canonical maps to itself)
_ESPN_TO_CANONICAL: Dict[str, str] = {a: a for a in TEAMS}
_ESPN_TO_CANONICAL.update({t[2]: a for a, t in TEAMS.items()})

# Odds API / human team-name -> canonical abbr (lowercase keys, explicit
# aliases; never regex-mangle names — that's how "Philadelphia 76ers" used to
# become unmatchable)
TEAMNAME_TO_ABBR: Dict[str, str] = {t[1].lower(): a for a, t in TEAMS.items()}
TEAMNAME_TO_ABBR.update({
    "los angeles clippers": "LAC",
    "la lakers": "LAL",
})


def normalize_espn_abbr(abbr: str) -> str:
    """ESPN variant (GS/NO/NY/SA/UTAH/WSH) or canonical -> canonical tricode."""
    a = (abbr or "").strip().upper()
    got = _ESPN_TO_CANONICAL.get(a)
    if got is None:
        raise ValueError(f"unknown team abbreviation: {abbr!r}")
    return got


def require_abbr(abbr: str) -> str:
    """Assert the abbr is already canonical; raise loudly otherwise."""
    a = (abbr or "").strip().upper()
    if a not in CANONICAL_ABBRS:
        raise ValueError(f"non-canonical team abbreviation: {abbr!r}")
    return a


def abbr_from_team_name(name: str) -> str | None:
    """Odds API full team name -> canonical abbr; None if unknown (caller logs)."""
    return TEAMNAME_TO_ABBR.get((name or "").strip().lower())


def seed_rows() -> List[tuple]:
    """Rows for seeding the `teams` table: (abbr, id, full_name, espn_abbr, conference)."""
    return [(a, t[0], t[1], t[2], t[3]) for a, t in sorted(TEAMS.items())]
