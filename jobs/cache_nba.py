#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import random
import datetime as dt
from typing import Dict, Any, List, Set, Optional, Tuple

import requests
import pandas as pd
import psycopg2


# ============================================================
# Time helpers
# ============================================================
def us_eastern_today() -> dt.date:
    try:
        from zoneinfo import ZoneInfo
        now_et = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        return now_et.date()
    except Exception:
        # fallback: UTC-5 approximation (ignores DST)
        return (dt.datetime.utcnow() - dt.timedelta(hours=5)).date()


def now_tw_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Taipei")
        return dt.datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# DB
# ============================================================
def db_connect():
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if db_url:
        return psycopg2.connect(db_url)

    host = (os.environ.get("SUPABASE_HOST") or "").strip()
    dbname = (os.environ.get("SUPABASE_DB") or "").strip()
    user = (os.environ.get("SUPABASE_USER") or "").strip()
    password = (os.environ.get("SUPABASE_PASSWORD") or "").strip()
    port = (os.environ.get("SUPABASE_PORT") or "5432").strip()

    if not all([host, dbname, user, password, port]):
        raise RuntimeError("DB env missing: set DATABASE_URL or SUPABASE_HOST/DB/USER/PASSWORD/PORT")

    return psycopg2.connect(
        host=host, dbname=dbname, user=user, password=password, port=int(port), sslmode="require"
    )


def ensure_schema(conn) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS public.nba_cache (
              cache_key TEXT PRIMARY KEY
            );
            """)
            cur.execute("ALTER TABLE public.nba_cache ADD COLUMN IF NOT EXISTS payload_json TEXT;")
            cur.execute("ALTER TABLE public.nba_cache ADD COLUMN IF NOT EXISTS updated_at_tw TEXT;")
    print("[INFO] schema ensured: nba_cache", flush=True)


def cache_put(conn, cache_key: str, payload: Dict[str, Any]) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.nba_cache(cache_key, payload_json, updated_at_tw)
                VALUES (%s, %s, %s)
                ON CONFLICT(cache_key) DO UPDATE SET
                  payload_json=EXCLUDED.payload_json,
                  updated_at_tw=EXCLUDED.updated_at_tw
                """,
                (cache_key, json.dumps(payload, ensure_ascii=False), now_tw_str()),
            )


def cache_put_if_nonempty(conn, cache_key: str, payload: Dict[str, Any], rows_len: int) -> bool:
    """
    Only write if rows_len > 0, to avoid overwriting good cache with empty data when stats.nba.com fails.
    Returns True if written, False if skipped.
    """
    if rows_len <= 0:
        print(f"[WARN] skip cache_put rows=0 key={cache_key}", flush=True)
        return False
    cache_put(conn, cache_key, payload)
    return True


# ============================================================
# ESPN: figure out needed teams
# ============================================================
def fetch_espn_scoreboard(session: requests.Session, date_us: dt.date) -> List[dict]:
    ymd = date_us.strftime("%Y%m%d")
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    r = session.get(url, params={"dates": ymd, "limit": 300}, timeout=(5, 25))
    r.raise_for_status()
    data = r.json()
    return data.get("events") or []


def teams_from_events(events: List[dict]) -> Set[str]:
    abbrs: Set[str] = set()
    for ev in events:
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        competitors = comp.get("competitors") or []
        for c in competitors:
            team = c.get("team") or {}
            ab = team.get("abbreviation")
            if ab:
                abbrs.add(ab)
    return abbrs


def build_needed_abbrs(session: requests.Session, anchor: dt.date, past_days: int, future_days: int) -> Set[str]:
    need: Set[str] = set()

    # Past: anchor, anchor-1, ..., anchor-(past_days-1)
    past_dates = [anchor - dt.timedelta(days=i) for i in range(past_days)]
    # Future: anchor+1, ..., anchor+future_days
    future_dates = [anchor + dt.timedelta(days=i) for i in range(1, future_days + 1)]

    dates = past_dates + future_dates

    for d in dates:
        try:
            ev = fetch_espn_scoreboard(session, d)
            got = teams_from_events(ev)
            need |= got
            print(f"[INFO] ESPN {d.isoformat()} teams={len(got)} total_need={len(need)}", flush=True)
        except Exception as e:
            print(f"[WARN] ESPN failed {d.isoformat()} err={e}", flush=True)

        # tiny jitter to be polite
        time.sleep(0.05 + random.random() * 0.05)

    return need


# ============================================================
# NBA Stats direct (avoid nba_api timeouts)
# ============================================================
NBA_STATS_BASE = "https://stats.nba.com/stats"

NBA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "Connection": "keep-alive",
}


def make_http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(NBA_HEADERS)
    return s


def _sleep_backoff(attempt: int, base: float, cap: float) -> None:
    # exponential backoff + jitter
    # attempt starts from 0
    expo = base * (2 ** attempt)
    jitter = random.uniform(0.0, base)
    time.sleep(min(cap, expo + jitter))


class CircuitBreaker:
    def __init__(self, fail_threshold: int = 6):
        self.fail_threshold = fail_threshold
        self.consecutive_failures = 0
        self.opened = False

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.fail_threshold:
            self.opened = True

    def allow(self) -> bool:
        return not self.opened


def nba_stats_get_json(
    session: requests.Session,
    endpoint: str,
    params: Dict[str, Any],
    *,
    timeout: Tuple[float, float] = (6, 20),
    retries: int = 2,
    backoff_base: float = 0.8,
    backoff_cap: float = 8.0,
    cb: Optional[CircuitBreaker] = None,
) -> Optional[dict]:
    """
    Robust GET for stats.nba.com with short timeouts, retries, backoff, and optional circuit breaker.
    Returns JSON dict or None if failed.
    """
    if cb and not cb.allow():
        print(f"[WARN] circuit breaker OPEN; skip stats endpoint={endpoint}", flush=True)
        return None

    url = f"{NBA_STATS_BASE}/{endpoint}"

    for attempt in range(retries + 1):
        try:
            # small randomness to avoid fixed request rhythm
            if random.random() < 0.15:
                time.sleep(random.uniform(0.1, 0.6))

            r = session.get(url, params=params, timeout=timeout)
            # Many failures are 429/5xx; treat non-200 as error to retry
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            if cb:
                cb.record_success()
            return data
        except Exception as e:
            if cb:
                cb.record_failure()
                if not cb.allow():
                    print(f"[WARN] circuit breaker OPEN after err={e}", flush=True)
                    return None

            if attempt < retries:
                print(f"[WARN] retry {attempt+1}/{retries} endpoint={endpoint} err={e}", flush=True)
                _sleep_backoff(attempt, backoff_base, backoff_cap)
            else:
                print(f"[WARN] FAILED endpoint={endpoint} err={e}", flush=True)
                return None

    return None


def resultset_to_df(data: dict, idx: int = 0) -> pd.DataFrame:
    """
    Convert NBA stats API resultSets to DataFrame.
    Works with typical structure:
      {"resultSets": [{"headers": [...], "rowSet": [...]}]}
    """
    try:
        rs = (data.get("resultSets") or [])[idx]
        headers = rs.get("headers") or []
        rows = rs.get("rowSet") or []
        return pd.DataFrame(rows, columns=headers)
    except Exception as e:
        print(f"[WARN] resultset_to_df failed idx={idx} err={e}", flush=True)
        return pd.DataFrame()


def fetch_league_dash_player_stats(
    session: requests.Session,
    season: str,
    cb: CircuitBreaker,
) -> pd.DataFrame:
    # Minimal stable parameter set; NBA endpoint accepts many params.
    params = {
        "Season": season,
        "SeasonType": "Regular Season",
        "PerMode": "PerGame",
        "MeasureType": "Base",
        "PlusMinus": "N",
        "PaceAdjust": "N",
        "Rank": "N",
        "LeagueID": "00",
        "Outcome": "",
        "Location": "",
        "Month": "0",
        "SeasonSegment": "",
        "DateFrom": "",
        "DateTo": "",
        "OpponentTeamID": "0",
        "VsConference": "",
        "VsDivision": "",
        "GameSegment": "",
        "Period": "0",
        "LastNGames": "0",
        "GameScope": "",
        "PlayerExperience": "",
        "PlayerPosition": "",
        "StarterBench": "",
        "DraftYear": "",
        "DraftPick": "",
        "College": "",
        "Country": "",
        "Height": "",
        "Weight": "",
        "TwoWay": "0",
    }
    data = nba_stats_get_json(
        session,
        "leaguedashplayerstats",
        params,
        timeout=(6, 22),
        retries=int(os.environ.get("NBA_STATS_RETRIES") or "2"),
        cb=cb,
    )
    if not data:
        return pd.DataFrame()
    return resultset_to_df(data, 0)


def fetch_team_game_log(
    session: requests.Session,
    season: str,
    team_id: int,
    cb: CircuitBreaker,
) -> pd.DataFrame:
    params = {
        "TeamID": str(team_id),
        "Season": season,
        "SeasonType": "Regular Season",
        "LeagueID": "00",
        "DateFrom": "",
        "DateTo": "",
    }
    data = nba_stats_get_json(
        session,
        "teamgamelog",
        params,
        timeout=(6, 20),
        retries=int(os.environ.get("NBA_STATS_RETRIES") or "2"),
        cb=cb,
    )
    if not data:
        return pd.DataFrame()
    return resultset_to_df(data, 0)


# ============================================================
# Team mapping (no nba_api dependency)
# ============================================================
# Use ESPN mapping first (from scoreboard events), fallback hardcoded NBA team abbreviations -> IDs via NBA official list.
# For reliability, keep a static mapping here.
ABBR_TO_TEAM_ID: Dict[str, int] = {
    "ATL": 1610612737,
    "BOS": 1610612738,
    "BKN": 1610612751,
    "CHA": 1610612766,
    "CHI": 1610612741,
    "CLE": 1610612739,
    "DAL": 1610612742,
    "DEN": 1610612743,
    "DET": 1610612765,
    "GS": 1610612744,     # ESPN uses "GS" (Warriors) sometimes as "GSW" depending feed; handle both below
    "GSW": 1610612744,
    "HOU": 1610612745,
    "IND": 1610612754,
    "LAC": 1610612746,
    "LAL": 1610612747,
    "MEM": 1610612763,
    "MIA": 1610612748,
    "MIL": 1610612749,
    "MIN": 1610612750,
    "NO": 1610612740,     # Pelicans
    "NOP": 1610612740,
    "NY": 1610612752,     # Knicks
    "NYK": 1610612752,
    "OKC": 1610612760,
    "ORL": 1610612753,
    "PHI": 1610612755,
    "PHX": 1610612756,
    "POR": 1610612757,
    "SA": 1610612759,
    "SAS": 1610612759,
    "SAC": 1610612758,
    "TOR": 1610612761,
    "UTAH": 1610612762,
    "UTA": 1610612762,
    "WSH": 1610612764,
}


def normalize_abbr(abbr: str) -> str:
    # ESPN abbreviations sometimes vary; normalize known ones.
    a = (abbr or "").strip().upper()
    if a in ("GSW", "GS"):
        return "GSW"
    if a in ("NOP", "NO"):
        return "NOP"
    if a in ("NY", "NYK"):
        return "NYK"
    if a in ("SA", "SAS"):
        return "SAS"
    if a in ("UTA", "UTAH"):
        return "UTA"
    return a


# ============================================================
# Main
# ============================================================
def main():
    # Config
    season = (os.environ.get("NBA_SEASON") or "2025-26").strip()

    override = (os.environ.get("OVERRIDE_US_DATE") or "").strip()
    if override:
        anchor = dt.datetime.strptime(override, "%m/%d/%Y").date()
    else:
        anchor = us_eastern_today()

    past_days = int((os.environ.get("CACHE_PAST_DAYS") or "7").strip())
    future_days = int((os.environ.get("CACHE_FUTURE_DAYS") or "7").strip())

    # Circuit breaker threshold: how many consecutive failures before stopping stats calls
    cb_threshold = int((os.environ.get("NBA_STATS_CB_THRESHOLD") or "6").strip())
    cb = CircuitBreaker(fail_threshold=cb_threshold)

    print(
        f"[INFO] cache start season={season} anchor_us={anchor.isoformat()} past={past_days} future={future_days}",
        flush=True,
    )

    # Sessions
    espn_sess = requests.Session()
    stats_sess = make_http_session()

    # DB connect once
    conn = db_connect()
    try:
        ensure_schema(conn)

        # 1) needed teams from ESPN
        needed_abbrs_raw = build_needed_abbrs(espn_sess, anchor, past_days=past_days, future_days=future_days)
        if not needed_abbrs_raw:
            cache_put(conn, "cache_meta", {"season": season, "updated_at_tw": now_tw_str(), "note": "no games found in window"})
            print("[OK] no teams needed; cache_meta written", flush=True)
            return

        needed_norm = sorted({normalize_abbr(a) for a in needed_abbrs_raw})
        print(f"[INFO] needed teams ({len(needed_norm)}): {needed_norm}", flush=True)

        # 2) player stats
        ps = fetch_league_dash_player_stats(stats_sess, season=season, cb=cb)
        wrote_ps = cache_put_if_nonempty(
            conn,
            f"player_stats:{season}",
            {"season": season, "rows": ps.to_dict(orient="records")},
            rows_len=len(ps),
        )
        if wrote_ps:
            print(f"[OK] cached player_stats rows={len(ps)}", flush=True)
        else:
            print(f"[WARN] player_stats not cached (rows=0). Keeping previous cache if exists.", flush=True)

        # 3) team logs (only those we can map to team_id)
        needed: List[str] = []
        for ab in needed_norm:
            # Map normalized abbrev to team_id via ABBR_TO_TEAM_ID
            tid = ABBR_TO_TEAM_ID.get(ab)
            if tid is None:
                # try raw key (just in case)
                tid = ABBR_TO_TEAM_ID.get(ab.replace(" ", ""))
            if tid is None:
                print(f"[WARN] unknown team abbr={ab}; skip", flush=True)
                continue
            needed.append(ab)

        print(f"[INFO] caching team_logs count={len(needed)}", flush=True)

        per_team_sleep = float((os.environ.get("NBA_TEAM_SLEEP") or "0.35").strip())

        for idx, abbr in enumerate(needed, start=1):
            tid = ABBR_TO_TEAM_ID[abbr]
            print(f"[INFO] ({idx}/{len(needed)}) fetching team_log {abbr} team_id={tid}", flush=True)

            # If circuit breaker already open, skip quickly
            if not cb.allow():
                print("[WARN] circuit breaker OPEN; stop fetching team logs", flush=True)
                break

            log_df = fetch_team_game_log(stats_sess, season=season, team_id=tid, cb=cb)

            wrote = cache_put_if_nonempty(
                conn,
                f"team_log:{season}:{abbr}",
                {"season": season, "abbr": abbr, "team_id": tid, "rows": log_df.to_dict(orient="records")},
                rows_len=len(log_df),
            )
            if wrote:
                print(f"[OK] cached team_log {abbr} rows={len(log_df)}", flush=True)
            else:
                print(f"[WARN] team_log {abbr} not cached (rows=0). Keeping previous cache if exists.", flush=True)

            # polite sleep + jitter
            time.sleep(per_team_sleep + random.random() * 0.15)

        # 4) meta
        cache_put(
            conn,
            "cache_meta",
            {
                "season": season,
                "anchor_us": anchor.strftime("%Y-%m-%d"),
                "window": {"past_days": past_days, "future_days": future_days},
                "teams_from_espn": sorted({normalize_abbr(a) for a in needed_abbrs_raw}),
                "teams_cached": needed,
                "stats_circuit_breaker": {
                    "fail_threshold": cb.fail_threshold,
                    "consecutive_failures": cb.consecutive_failures,
                    "opened": cb.opened,
                },
                "updated_at_tw": now_tw_str(),
            },
        )
        print("[OK] cache complete", flush=True)

    finally:
        try:
            conn.close()
        except Exception as e:
            print(f"[WARN] db close failed err={e}", flush=True)


if __name__ == "__main__":
    main()
