#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
jobs/sync_daily.py

What it does:
- Fetch NBA scoreboard (schedule/scores/status) from ESPN for a date window
- Fetch spreads from The Odds API with multi-bookmaker fallback
- Upsert into Supabase/Postgres public.games (schema-aligned)

Backfill:
- BACKFILL_PAST_DAYS: e.g. "7"  -> anchor day and previous 6 days
- BACKFILL_FUTURE_DAYS: e.g. "7" -> anchor day and next 6 days
- Anchor day defaults to US/Eastern "today", or OVERRIDE_US_DATE (MM/DD/YYYY)

Important behavior:
- For PAST dates: if odds not found, we DO NOT overwrite existing odds in DB
  (we send NULLs and UPSERT uses COALESCE to preserve existing values)
- For TODAY/FUTURE dates: if odds not found, we write fallback (0, 1.90, 1.90, "Fallback ⚠️")

Env expected by your workflow:
- SUPABASE_HOST, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD, SUPABASE_PORT
- ODDS_API_KEY
- NBA_SEASON (optional)
- OVERRIDE_US_DATE (optional, MM/DD/YYYY) from workflow input verify_us_date
- BACKFILL_PAST_DAYS (optional int, default 1)
- BACKFILL_FUTURE_DAYS (optional int, default 1)
- DRY_RUN=1 (optional)
"""

import os
import re
import datetime as dt
from typing import Dict, Tuple, Optional, List, Any

import requests
import psycopg2
import psycopg2.extras


# -----------------------------
# Utilities
# -----------------------------

def norm_name(s: str) -> str:
    if s is None:
        return ""
    s = s.strip().lower()
    s = s.replace("&", "and")
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def us_eastern_today() -> dt.date:
    """Exact if zoneinfo available; else approximate UTC-5."""
    try:
        from zoneinfo import ZoneInfo
        now_et = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        return now_et.date()
    except Exception:
        return (dt.datetime.utcnow() - dt.timedelta(hours=5)).date()


def now_tw_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Taipei")
        return dt.datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def today_tw_mmddyyyy() -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Taipei")
        return dt.datetime.now(tz=tz).strftime("%m/%d/%Y")
    except Exception:
        return (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%m/%d/%Y")


# -----------------------------
# Team mapping (Odds API -> Abbr)
# -----------------------------

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

BOOK_KEY_ALIASES = {"pointsbet": "pointsbetus"}


# -----------------------------
# ESPN Fetch (scoreboard)
# -----------------------------

def fetch_espn_scoreboard(date_us: dt.date) -> List[dict]:
    """
    Stable endpoint:
    https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD
    """
    ymd = date_us.strftime("%Y%m%d")
    candidates = [
        ("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", {"dates": ymd}),
        ("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", {"dates": ymd, "limit": 300}),
        ("https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", {"dates": ymd}),
        ("https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", {"dates": ymd, "limit": 300}),
    ]

    last_err: Optional[Exception] = None
    for url, params in candidates:
        try:
            r = requests.get(url, params=params, timeout=25)
            if r.status_code == 200:
                data = r.json()
                events = data.get("events") or []
                print(f"[INFO] espn scoreboard ok url={url} games={len(events)}")
                return events
            print(f"[WARN] espn scoreboard status={r.status_code} url={url} body={r.text[:120]}")
            last_err = Exception(f"status={r.status_code} url={r.url}")
        except Exception as e:
            print(f"[WARN] espn scoreboard error url={url} err={e}")
            last_err = e

    raise RuntimeError(f"espn scoreboard failed after fallbacks: {last_err}")


def parse_espn_events(events: List[dict], date_us: dt.date) -> List[dict]:
    """
    Output per game:
    - game_date_us (MM/DD/YYYY)
    - home_abbr, away_abbr
    - home_name, away_name  (ESPN displayName)
    - home_score, away_score (int or None)
    - status: scheduled | in_progress | final
    """
    out: List[dict] = []
    game_date_str = date_us.strftime("%m/%d/%Y")

    for ev in events:
        try:
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

            home_name = home_team.get("displayName") or home_team.get("shortDisplayName") or home_abbr
            away_name = away_team.get("displayName") or away_team.get("shortDisplayName") or away_abbr

            st = (comp.get("status") or {}).get("type") or {}
            state = (st.get("state") or "").lower()  # pre|in|post
            completed = bool(st.get("completed"))

            if completed or state == "post":
                status = "final"
            elif state == "in":
                status = "in_progress"
            else:
                status = "scheduled"

            home_score = None
            away_score = None
            if status in ("final", "in_progress"):
                try:
                    if home.get("score") is not None:
                        home_score = int(home.get("score"))
                    if away.get("score") is not None:
                        away_score = int(away.get("score"))
                except Exception:
                    home_score = None
                    away_score = None

            out.append({
                "game_date_us": game_date_str,
                "home_abbr": home_abbr,
                "away_abbr": away_abbr,
                "home_name": home_name,
                "away_name": away_name,
                "home_score": home_score,
                "away_score": away_score,
                "status": status,
            })
        except Exception:
            continue

    return out


# -----------------------------
# Odds API Fetch (multi-book fallback)
# -----------------------------

def get_odds_map() -> Dict[Tuple[str, str], dict]:
    """
    Map keyed by (away_abbr, home_abbr):
      {
        (away, home): {
          home_spread, home_odds, away_odds, line_source
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

    def _norm_book_key(k: str) -> str:
        nk = norm_name(k)
        return BOOK_KEY_ALIASES.get(nk, nk)

    def pick_best_market(bookmakers: list) -> Tuple[Optional[str], Optional[dict]]:
        if not bookmakers:
            return None, None

        key_to_b = {_norm_book_key(b.get("key", "")): b for b in bookmakers if isinstance(b, dict)}

        for bk in wanted_books:
            b = key_to_b.get(bk)
            if not b:
                continue
            for m in (b.get("markets") or []):
                if m.get("key") == "spreads":
                    return bk, m

        for b in bookmakers:
            for m in (b.get("markets") or []):
                if m.get("key") == "spreads":
                    bk_guess = _norm_book_key(b.get("key", "")) or norm_name(b.get("title", ""))
                    return bk_guess or "unknown", m

        return None, None

    out: Dict[Tuple[str, str], dict] = {}

    for g in data:
        try:
            home_name = norm_name(g.get("home_team", ""))
            away_name = norm_name(g.get("away_team", ""))

            home_abbr = ODDS_TEAMNAME_TO_ABBR.get(home_name)
            away_abbr = ODDS_TEAMNAME_TO_ABBR.get(away_name)
            if not home_abbr or not away_abbr:
                continue

            bk_key, spreads = pick_best_market(g.get("bookmakers") or [])
            if not spreads:
                continue

            home_spread = None
            home_odds = None
            away_odds = None

            for o in (spreads.get("outcomes") or []):
                nm = norm_name(o.get("name", ""))
                pt = o.get("point", None)
                pr = o.get("price", None)
                if pt is None or pr is None:
                    continue
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
# DB
# -----------------------------

def db_connect():
    # Prefer DATABASE_URL if you ever add it
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if db_url:
        return psycopg2.connect(db_url)

    host = (os.environ.get("SUPABASE_HOST") or "").strip()
    dbname = (os.environ.get("SUPABASE_DB") or "").strip()
    user = (os.environ.get("SUPABASE_USER") or "").strip()
    password = (os.environ.get("SUPABASE_PASSWORD") or "").strip()
    port = (os.environ.get("SUPABASE_PORT") or "5432").strip()

    present = all([host, dbname, user, password, port])
    print(f"[INFO] DB_ENV_present={present} via={'SUPABASE_*' if present else 'none'}")
    if not present:
        raise RuntimeError("DB connection env missing: set DATABASE_URL or SUPABASE_HOST/DB/USER/PASSWORD/PORT")

    return psycopg2.connect(
        host=host,
        dbname=dbname,
        user=user,
        password=password,
        port=int(port),
        sslmode="require",
    )


# Schema-aligned UPSERT (your columns) + ON CONFLICT(game_id) + COALESCE preserve logic
UPSERT_SQL = """
INSERT INTO public.games (
    game_id,
    game_date_us,
    season,
    away_abbr,
    home_abbr,
    away_name,
    home_name,
    home_spread,
    home_odds,
    away_odds,
    line_source,
    status,
    away_score,
    home_score,
    created_at_tw,
    updated_at_tw,
    game_date_tw
) VALUES (
    %(game_id)s,
    %(game_date_us)s,
    %(season)s,
    %(away_abbr)s,
    %(home_abbr)s,
    %(away_name)s,
    %(home_name)s,
    %(home_spread)s,
    %(home_odds)s,
    %(away_odds)s,
    %(line_source)s,
    %(status)s,
    %(away_score)s,
    %(home_score)s,
    %(created_at_tw)s,
    %(updated_at_tw)s,
    %(game_date_tw)s
)
ON CONFLICT (game_id)
DO UPDATE SET
    game_date_us  = EXCLUDED.game_date_us,
    season        = EXCLUDED.season,
    away_abbr     = EXCLUDED.away_abbr,
    home_abbr     = EXCLUDED.home_abbr,
    away_name     = EXCLUDED.away_name,
    home_name     = EXCLUDED.home_name,

    -- Preserve existing odds if EXCLUDED is NULL (useful for past backfill when odds not available)
    home_spread   = COALESCE(EXCLUDED.home_spread, public.games.home_spread),
    home_odds     = COALESCE(EXCLUDED.home_odds,   public.games.home_odds),
    away_odds     = COALESCE(EXCLUDED.away_odds,   public.games.away_odds),
    line_source   = COALESCE(EXCLUDED.line_source, public.games.line_source),

    status        = EXCLUDED.status,
    away_score    = EXCLUDED.away_score,
    home_score    = EXCLUDED.home_score,

    updated_at_tw = EXCLUDED.updated_at_tw,
    game_date_tw  = EXCLUDED.game_date_tw;
"""


def upsert_games(rows: List[dict]) -> None:
    if (os.environ.get("DRY_RUN") or "").strip() == "1":
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
    # Safe env presence logs
    print(f"[INFO] ODDS_API_KEY_present={bool((os.environ.get('ODDS_API_KEY') or '').strip())}")

    # Anchor date (US)
    override = (os.environ.get("OVERRIDE_US_DATE") or "").strip()   # MM/DD/YYYY from workflow input
    if override:
        try:
            anchor_date_us = dt.datetime.strptime(override, "%m/%d/%Y").date()
        except ValueError:
            raise RuntimeError("OVERRIDE_US_DATE must be MM/DD/YYYY")
    else:
        anchor_date_us = us_eastern_today()

    print(f"[INFO] target_date_us={anchor_date_us.isoformat()}")

    # Backfill windows
    def _int_env(name: str, default: int) -> int:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            return default
        try:
            return max(1, int(raw))
        except ValueError:
            raise RuntimeError(f"{name} must be an integer")

    past_days = _int_env("BACKFILL_PAST_DAYS", 1)      # include anchor day
    future_days = _int_env("BACKFILL_FUTURE_DAYS", 1)  # include anchor day

    # Build date list: past (anchor to anchor-(past-1)), plus future (anchor+1 to anchor+(future-1))
    past_list = [anchor_date_us - dt.timedelta(days=i) for i in range(past_days)]
    future_list = [anchor_date_us + dt.timedelta(days=i) for i in range(1, future_days)]
    date_list = past_list + future_list

    print(f"[INFO] backfill_past_days={past_days} backfill_future_days={future_days}")
    print(f"[INFO] dates={[d.isoformat() for d in date_list]}")

    season = (os.environ.get("NBA_SEASON") or "2025-26").strip()
    ts_tw = now_tw_str()
    game_date_tw = today_tw_mmddyyyy()

    # Odds map (current market snapshot) once per run
    odds_map = get_odds_map()

    total_rows = 0
    for d in date_list:
        print(f"[INFO] ---- sync date_us={d.isoformat()} ----")

        # ESPN for each date
        try:
            events = fetch_espn_scoreboard(d)
            games = parse_espn_events(events, d)
            print(f"[INFO] espn games={len(games)}")
        except Exception as e:
            print(f"[ERROR] espn fetch failed for {d.isoformat()}: {e}")
            continue

        rows: List[dict] = []

        # Rule:
        # - For past dates (< anchor): if odds not found -> set NULL so DB keeps existing odds
        # - For anchor/today/future (>= anchor): if odds not found -> set fallback values
        is_past = d < anchor_date_us

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
                if is_past:
                    sp, oh, oa, src = None, None, None, None   # preserve existing
                else:
                    sp, oh, oa, src = 0.0, 1.90, 1.90, "Fallback ⚠️"

            game_id = f"{d.strftime('%Y%m%d')}_{away_abbr}_{home_abbr}"

            rows.append({
                "game_id": game_id,
                "game_date_us": g["game_date_us"],   # MM/DD/YYYY
                "season": season,

                "away_abbr": away_abbr,
                "home_abbr": home_abbr,
                "away_name": g.get("away_name") or away_abbr,
                "home_name": g.get("home_name") or home_abbr,

                "home_spread": sp,
                "home_odds": oh,
                "away_odds": oa,
                "line_source": src,

                "status": g["status"],
                "away_score": g["away_score"],
                "home_score": g["home_score"],

                "created_at_tw": ts_tw,
                "updated_at_tw": ts_tw,
                "game_date_tw": game_date_tw,
            })

        if rows:
            upsert_games(rows)
            total_rows += len(rows)

    print(f"[INFO] backfill done total_rows={total_rows}")


if __name__ == "__main__":
    main()
