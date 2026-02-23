#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
sync_daily.py
- Fetch NBA games (schedule + final scores) from ESPN
- Fetch spreads odds from The Odds API with multi-bookmaker fallback
- Upsert into Postgres (DATABASE_URL)
- Debug logs for ODDS_API_KEY presence and odds mapping

Required env:
- DATABASE_URL: Postgres connection string, e.g. postgres://user:pass@host:5432/dbname
Optional env:
- ODDS_API_KEY: The Odds API key
- TARGET_DATE_US: YYYY-MM-DD (US date). If not provided, use US/Eastern "today"
- DRY_RUN: "1" to skip DB write
"""

import os
import re
import json
import time
import datetime as dt
from typing import Dict, Tuple, Optional, Any, List

import requests

# psycopg2 is commonly available in GH actions python env; if you use psycopg (v3) or sqlalchemy, swap accordingly
import psycopg2
import psycopg2.extras


# -----------------------------
# Utilities
# -----------------------------

def norm_name(s: str) -> str:
    """Normalize team/book names to stable keys."""
    if s is None:
        return ""
    s = s.strip().lower()
    s = s.replace("&", "and")
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def us_eastern_today() -> dt.date:
    """
    Compute "today" in US/Eastern without external tz libs.
    Good enough for NBA daily sync scheduling:
    - During DST, Eastern is UTC-4; otherwise UTC-5.
    If you want exact DST correctness, add zoneinfo (Python 3.9+) with America/New_York.
    """
    try:
        from zoneinfo import ZoneInfo
        now_et = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        return now_et.date()
    except Exception:
        # fallback: approximate using UTC-5
        now_utc = dt.datetime.utcnow()
        return (now_utc - dt.timedelta(hours=5)).date()


# -----------------------------
# Team mapping
# -----------------------------
# ESPN uses abbreviations like "LAL", "BOS", etc.
# Odds API uses full team names; we map them to abbr.
# Expand this mapping if you see "mapped=0" even when odds api has games.
ODDS_TEAMNAME_TO_ABBR: Dict[str, str] = {
    "atlanta hawks": "ATL",
    "boston celtics": "BOS",
    "brooklyn nets": "BKN",
    "charlotte hornets": "CHA",
    "chicago bulls": "CHI",
    "cleveland cavaliers": "CLE",
    "dallas mavericks": "DAL",
    "denver nuggets": "DEN",
    "detroit pistons": "DET",
    "golden state warriors": "GSW",
    "houston rockets": "HOU",
    "indiana pacers": "IND",
    "la clippers": "LAC",
    "los angeles clippers": "LAC",
    "la lakers": "LAL",
    "los angeles lakers": "LAL",
    "memphis grizzlies": "MEM",
    "miami heat": "MIA",
    "milwaukee bucks": "MIL",
    "minnesota timberwolves": "MIN",
    "new orleans pelicans": "NOP",
    "new york knicks": "NYK",
    "oklahoma city thunder": "OKC",
    "orlando magic": "ORL",
    "philadelphia 76ers": "PHI",
    "phoenix suns": "PHX",
    "portland trail blazers": "POR",
    "sacramento kings": "SAC",
    "san antonio spurs": "SAS",
    "toronto raptors": "TOR",
    "utah jazz": "UTA",
    "washington wizards": "WAS",
}

# Some books keys differ; normalize them
BOOK_KEY_ALIASES = {
    "pointsbet": "pointsbetus",
}


# -----------------------------
# ESPN Fetch
# -----------------------------

def fetch_espn_scoreboard(date_us: dt.date) -> List[dict]:
    """
    ESPN scoreboard endpoint:
    https://site.web.api.espn.com/apis/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD
    Returns list of "events".
    """
    ymd = date_us.strftime("%Y%m%d")
    url = "https://site.web.api.espn.com/apis/v2/sports/basketball/nba/scoreboard"
    params = {"dates": ymd, "limit": 300}

    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    events = data.get("events") or []
    return events


def parse_espn_events(events: List[dict], date_us: dt.date) -> List[dict]:
    """
    Convert ESPN events into a normalized game list.

    Output fields:
    - game_date_us (MM/DD/YYYY)
    - start_time_utc (iso string) if available
    - home_abbr, away_abbr
    - home_score, away_score (None if not final)
    - status ("scheduled"|"in_progress"|"final")
    - espn_event_id
    """
    out = []
    game_date_str = date_us.strftime("%m/%d/%Y")

    for ev in events:
        try:
            ev_id = str(ev.get("id") or "")
            competitions = ev.get("competitions") or []
            if not competitions:
                continue
            comp = competitions[0]
            competitors = comp.get("competitors") or []
            if len(competitors) < 2:
                continue

            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue

            home_team = home.get("team") or {}
            away_team = away.get("team") or {}
            home_abbr = home_team.get("abbreviation")
            away_abbr = away_team.get("abbreviation")
            if not home_abbr or not away_abbr:
                continue

            # status
            st = (comp.get("status") or {}).get("type") or {}
            state = (st.get("state") or "").lower()  # "pre", "in", "post"
            completed = bool(st.get("completed"))

            if completed or state == "post":
                status = "final"
            elif state == "in":
                status = "in_progress"
            else:
                status = "scheduled"

            # scores
            home_score = None
            away_score = None
            try:
                if status in ("final", "in_progress"):
                    home_score = int(home.get("score")) if home.get("score") is not None else None
                    away_score = int(away.get("score")) if away.get("score") is not None else None
            except Exception:
                home_score = None
                away_score = None

            start_time_utc = None
            # ESPN provides date in ISO with timezone in some fields
            # Often comp["date"] is ISO string
            if comp.get("date"):
                start_time_utc = comp["date"]

            out.append({
                "game_date_us": game_date_str,
                "start_time_utc": start_time_utc,
                "home_abbr": home_abbr,
                "away_abbr": away_abbr,
                "home_score": home_score,
                "away_score": away_score,
                "status": status,
                "espn_event_id": ev_id,
            })
        except Exception:
            continue

    return out


# -----------------------------
# Odds API Fetch (multi-book fallback)
# -----------------------------

def get_odds_map() -> Dict[Tuple[str, str], dict]:
    """
    Returns map keyed by (away_abbr, home_abbr):
    {
      (away_abbr, home_abbr): {
        "home_spread": float,
        "home_odds": float,
        "away_odds": float,
        "line_source": "OddsAPI:<bookkey>",
      }
    }
    """
    api_key = (os.environ.get("ODDS_API_KEY") or "").strip()
    if not api_key:
        print("[WARN] ODDS_API_KEY missing -> odds disabled")
        return {}

    url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"

    wanted_books = ["pinnacle", "draftkings", "fanduel", "betmgm", "caesars", "pointsbetus"]
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "spreads",
        "bookmakers": ",".join(wanted_books),
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        if r.status_code != 200:
            print(f"[WARN] odds api status={r.status_code} body={r.text[:200]}")
            return {}
        data = r.json()
        print(f"[INFO] odds api ok games={len(data)}")
    except Exception as e:
        print(f"[WARN] odds api error: {e}")
        return {}

    out: Dict[Tuple[str, str], dict] = {}

    def _norm_book_key(k: str) -> str:
        nk = norm_name(k)
        return BOOK_KEY_ALIASES.get(nk, nk)

    def pick_best_market(bookmakers: list) -> Tuple[Optional[str], Optional[dict]]:
        """
        Prefer wanted_books order, pick first book that has markets->spreads.
        """
        if not bookmakers:
            return None, None

        key_to_b = {_norm_book_key(b.get("key", "")): b for b in bookmakers if isinstance(b, dict)}

        for bk in wanted_books:
            b = key_to_b.get(bk)
            if not b:
                continue
            for m in b.get("markets", []) or []:
                if m.get("key") == "spreads":
                    return bk, m

        # fallback: any bookmaker with spreads
        for b in bookmakers:
            for m in b.get("markets", []) or []:
                if m.get("key") == "spreads":
                    bk_guess = _norm_book_key(b.get("key", "")) or norm_name(b.get("title", ""))
                    return bk_guess or "unknown", m

        return None, None

    for g in data:
        try:
            home_name = norm_name(g.get("home_team", ""))
            away_name = norm_name(g.get("away_team", ""))

            home_abbr = ODDS_TEAMNAME_TO_ABBR.get(home_name)
            away_abbr = ODDS_TEAMNAME_TO_ABBR.get(away_name)
            if not home_abbr or not away_abbr:
                continue

            bk_key, spreads = pick_best_market(g.get("bookmakers", []) or [])
            if not spreads:
                continue

            home_spread = None
            home_odds = None
            away_odds = None

            for o in spreads.get("outcomes", []) or []:
                nm = norm_name(o.get("name", ""))
                pt = o.get("point", None)
                pr = o.get("price", None)
                if pt is None or pr is None:
                    continue

                # point is usually from the team perspective.
                # We'll store "home_spread" as the home team's point value.
                if nm == home_name:
                    home_spread = float(pt)
                    home_odds = float(pr)
                elif nm == away_name:
                    away_odds = float(pr)

            if home_spread is None or home_odds is None or away_odds is None:
                continue

            out[(away_abbr, home_abbr)] = {
                "home_spread": float(home_spread),
                "home_odds": float(home_odds),
                "away_odds": float(away_odds),
                "line_source": f"OddsAPI:{bk_key}",
            }
        except Exception:
            continue

    print(f"[INFO] odds mapped={len(out)}")
    return out


# -----------------------------
# DB Upsert
# -----------------------------

UPSERT_SQL = """
INSERT INTO public.games (
    game_date_us,
    home_abbr,
    away_abbr,
    start_time_utc,
    home_score,
    away_score,
    status,
    espn_event_id,
    home_spread,
    home_odds,
    away_odds,
    line_source,
    updated_at
) VALUES (
    %(game_date_us)s,
    %(home_abbr)s,
    %(away_abbr)s,
    %(start_time_utc)s,
    %(home_score)s,
    %(away_score)s,
    %(status)s,
    %(espn_event_id)s,
    %(home_spread)s,
    %(home_odds)s,
    %(away_odds)s,
    %(line_source)s,
    NOW()
)
ON CONFLICT (game_date_us, home_abbr, away_abbr)
DO UPDATE SET
    start_time_utc = EXCLUDED.start_time_utc,
    home_score     = EXCLUDED.home_score,
    away_score     = EXCLUDED.away_score,
    status         = EXCLUDED.status,
    espn_event_id  = EXCLUDED.espn_event_id,
    home_spread    = EXCLUDED.home_spread,
    home_odds      = EXCLUDED.home_odds,
    away_odds      = EXCLUDED.away_odds,
    line_source    = EXCLUDED.line_source,
    updated_at     = NOW();
"""


def db_connect():
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not db_url:
        raise RuntimeError("DATABASE_URL missing")
    return psycopg2.connect(db_url)


def upsert_games(rows: List[dict]) -> None:
    if os.environ.get("DRY_RUN", "").strip() == "1":
        print(f"[DRY_RUN] skip db upsert rows={len(rows)}")
        return

    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, UPSERT_SQL, rows, page_size=100)
        print(f"[INFO] db upsert ok rows={len(rows)}")
    finally:
        conn.close()


# -----------------------------
# Main
# -----------------------------

def main():
    # Step 1: print key presence (safe)
    print(f"[INFO] ODDS_API_KEY_present={bool((os.environ.get('ODDS_API_KEY') or '').strip())}")

    # Determine target US date
    target = (os.environ.get("TARGET_DATE_US") or "").strip()
    if target:
        try:
            date_us = dt.datetime.strptime(target, "%Y-%m-%d").date()
        except ValueError:
            raise RuntimeError("TARGET_DATE_US must be YYYY-MM-DD")
    else:
        date_us = us_eastern_today()

    print(f"[INFO] target_date_us={date_us.isoformat()}")

    # Fetch ESPN games
    try:
        events = fetch_espn_scoreboard(date_us)
        games = parse_espn_events(events, date_us)
        print(f"[INFO] espn games={len(games)}")
    except Exception as e:
        print(f"[ERROR] espn fetch failed: {e}")
        raise

    # Fetch odds map
    odds_map = get_odds_map()

    # Merge odds into games rows
    rows = []
    for g in games:
        away_abbr = g["away_abbr"]
        home_abbr = g["home_abbr"]

        od = odds_map.get((away_abbr, home_abbr))
        if od:
            sp = float(od["home_spread"])
            oh = float(od["home_odds"])
            oa = float(od["away_odds"])
            src = od["line_source"]
        else:
            sp, oh, oa, src = 0.0, 1.90, 1.90, "Fallback ⚠️"

        row = {
            "game_date_us": g["game_date_us"],
            "home_abbr": home_abbr,
            "away_abbr": away_abbr,
            "start_time_utc": g["start_time_utc"],
            "home_score": g["home_score"],
            "away_score": g["away_score"],
            "status": g["status"],
            "espn_event_id": g["espn_event_id"],
            "home_spread": sp,
            "home_odds": oh,
            "away_odds": oa,
            "line_source": src,
        }
        rows.append(row)

    # Upsert to DB
    upsert_games(rows)


if __name__ == "__main__":
    main()
