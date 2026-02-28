#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import math
import time
import unicodedata
import datetime as dt
from typing import Dict, Tuple, Optional, List, Any

import requests
import psycopg2
from jobs.db_utils import db_connect
from jobs.time_utils import now_tw_str, today_tw_mmddyyyy, us_eastern_today
import psycopg2.extras
import pandas as pd
from bs4 import BeautifulSoup


def log_db_env_status() -> None:
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    host = (os.environ.get("SUPABASE_HOST") or "").strip()
    dbname = (os.environ.get("SUPABASE_DB") or "").strip()
    user = (os.environ.get("SUPABASE_USER") or "").strip()
    password = (os.environ.get("SUPABASE_PASSWORD") or "").strip()
    port = (os.environ.get("SUPABASE_PORT") or "").strip()

    print(
        "[INFO] db env "
        f"DATABASE_URL={'set' if bool(db_url) else 'missing'} "
        f"SUPABASE_HOST={'set' if bool(host) else 'missing'} "
        f"SUPABASE_DB={'set' if bool(dbname) else 'missing'} "
        f"SUPABASE_USER={'set' if bool(user) else 'missing'} "
        f"SUPABASE_PASSWORD={'set' if bool(password) else 'missing'} "
        f"SUPABASE_PORT={'set' if bool(port) else 'missing'}",
        flush=True,
    )


def norm_name(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    s = re.sub(r"[^a-z\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


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



def ensure_schema():
    ddl = """
    CREATE TABLE IF NOT EXISTS public.nba_cache (
      cache_key TEXT PRIMARY KEY,
      payload_json TEXT,
      updated_at_tw TEXT
    );

    CREATE TABLE IF NOT EXISTS public.model_registry (
      model_name TEXT PRIMARY KEY,
      model_version TEXT,
      payload_base64 TEXT,
      trained_rows INT,
      metrics JSONB,
      created_at_tw TEXT
    );

    CREATE TABLE IF NOT EXISTS public.games (
        game_id TEXT PRIMARY KEY,
        game_date_us TEXT,
        season TEXT,
        away_abbr TEXT,
        home_abbr TEXT,
        away_name TEXT,
        home_name TEXT,

        home_spread DOUBLE PRECISION,
        home_odds DOUBLE PRECISION,
        away_odds DOUBLE PRECISION,
        line_source TEXT,

        status TEXT,
        away_score INTEGER,
        home_score INTEGER,

        margin INTEGER,
        cover INTEGER,
        settled_at_tw TEXT,

        home_pts_sum DOUBLE PRECISION,
        away_pts_sum DOUBLE PRECISION,
        home_impact_mean DOUBLE PRECISION,
        away_impact_mean DOUBLE PRECISION,
        home_b2b INTEGER,
        away_b2b INTEGER,
        home_recent_w DOUBLE PRECISION,
        away_recent_w DOUBLE PRECISION,

        base_diff DOUBLE PRECISION,
        f_edge DOUBLE PRECISION,
        cover_prob DOUBLE PRECISION,
        implied_prob DOUBLE PRECISION,
        edge_value DOUBLE PRECISION,
        ev DOUBLE PRECISION,
        pick_team TEXT,
        odds_used DOUBLE PRECISION,

        created_at_tw TEXT,
        updated_at_tw TEXT,
        game_date_tw TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_games_date_us ON public.games (game_date_us);
    """
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
                cur.execute("""
                DO $$
                BEGIN
                  IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='games' AND column_name='home_b2b' AND data_type='boolean'
                  ) THEN
                    ALTER TABLE public.games
                    ALTER COLUMN home_b2b TYPE INTEGER
                    USING (CASE WHEN home_b2b THEN 1 ELSE 0 END);
                  END IF;

                  IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='games' AND column_name='away_b2b' AND data_type='boolean'
                  ) THEN
                    ALTER TABLE public.games
                    ALTER COLUMN away_b2b TYPE INTEGER
                    USING (CASE WHEN away_b2b THEN 1 ELSE 0 END);
                  END IF;
                END $$;
                """)
                cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_ts_pct DOUBLE PRECISION;")
                cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_ts_pct DOUBLE PRECISION;")
                cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_orb_rate DOUBLE PRECISION;")
                cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_orb_rate DOUBLE PRECISION;")
                cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_usage_proxy DOUBLE PRECISION;")
                cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_usage_proxy DOUBLE PRECISION;")
                cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_onoff_proxy DOUBLE PRECISION;")
                cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_onoff_proxy DOUBLE PRECISION;")
        print("[INFO] schema ensured")
    finally:
        conn.close()


def cache_get(cache_key: str) -> Optional[dict]:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT payload_json FROM public.nba_cache WHERE cache_key=%s", (cache_key,))
            row = cur.fetchone()
            if not row or not row[0]:
                return None
            return json.loads(row[0])
    finally:
        conn.close()


def load_models() -> Tuple[Optional[Any], Optional[Any]]:
    # stored as pickle(base64) by training jobs
    import base64, pickle
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT model_name, payload_base64 FROM public.model_registry")
            rows = cur.fetchall()
        base_model, calibrator = None, None
        for name, b64 in rows:
            if not b64:
                continue
            obj = pickle.loads(base64.b64decode(b64))
            if name == "margin_base_model":
                base_model = obj
            elif name == "cover_prob_calibrator":
                calibrator = obj
        return base_model, calibrator
    except Exception as e:
        print(f"[WARN] load_models failed err={e}")
        return None, None
    finally:
        conn.close()


# ---------------- ESPN scoreboard ----------------

def fetch_espn_scoreboard(date_us: dt.date) -> List[dict]:
    ymd = date_us.strftime("%Y%m%d")
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    r = requests.get(url, params={"dates": ymd, "limit": 300}, timeout=25)
    r.raise_for_status()
    data = r.json()
    return data.get("events") or []


def parse_espn_events(events: List[dict], date_us: dt.date) -> List[dict]:
    out: List[dict] = []
    game_date_str = date_us.strftime("%m/%d/%Y")

    for ev in events:
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

        home_name = home_team.get("displayName") or home_abbr
        away_name = away_team.get("displayName") or away_abbr

        st = (comp.get("status") or {}).get("type") or {}
        state = (st.get("state") or "").lower()
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
                home_score = int(home.get("score")) if home.get("score") is not None else None
                away_score = int(away.get("score")) if away.get("score") is not None else None
            except Exception as e:
                print(f"[WARN] score parse failed date={game_date_str} home={home_abbr} away={away_abbr} err={e}")
                home_score, away_score = None, None

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

    return out


# ---------------- Odds API (current only) ----------------

def get_odds_map() -> Dict[Tuple[str, str], dict]:
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
        except Exception as e:
            print(f"[WARN] odds mapping failed game={g.get('id', 'unknown')} home={g.get('home_team')} away={g.get('away_team')} err={e}")
            continue

    print(f"[INFO] odds mapped={len(out)}")
    return out


# ---------------- injuries (ESPN HTML) ----------------

TEAM_MAP = {
    "ATL": ["Atlanta Hawks"], "BKN": ["Brooklyn Nets"], "BOS": ["Boston Celtics"],
    "CHA": ["Charlotte Hornets"], "CHI": ["Chicago Bulls"], "CLE": ["Cleveland Cavaliers"],
    "DAL": ["Dallas Mavericks"], "DEN": ["Denver Nuggets"], "DET": ["Detroit Pistons"],
    "GSW": ["Golden State Warriors"], "HOU": ["Houston Rockets"], "IND": ["Indiana Pacers"],
    "LAC": ["LA Clippers"], "LAL": ["Los Angeles Lakers"], "MEM": ["Memphis Grizzlies"],
    "MIA": ["Miami Heat"], "MIL": ["Milwaukee Bucks"], "MIN": ["Minnesota Timberwolves"],
    "NOP": ["New Orleans Pelicans"], "NYK": ["New York Knicks"], "OKC": ["Oklahoma City Thunder"],
    "ORL": ["Orlando Magic"], "PHI": ["Philadelphia 76ers"], "PHX": ["Phoenix Suns"],
    "POR": ["Portland Trail Blazers"], "SAC": ["Sacramento Kings"], "SAS": ["San Antonio Spurs"],
    "TOR": ["Toronto Raptors"], "UTA": ["Utah Jazz"], "WAS": ["Washington Wizards"],
}

def get_injuries() -> pd.DataFrame:
    inj_list = []
    try:
        url = "https://www.espn.com/nba/injuries"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        tables = soup.select(".ResponsiveTable") or soup.select("section")
        for table in tables:
            title_el = table.select_one(".Table__Title") or table.find(["h2", "h3"])
            if not title_el:
                continue
            t_name = title_el.get_text(strip=True).lower()

            t_abbr = None
            for abbr, info in TEAM_MAP.items():
                if info[0].lower() in t_name:
                    t_abbr = abbr
                    break
            if not t_abbr:
                continue

            rows = table.select("tbody tr") if table.select("tbody tr") else table.select("tr")
            for r in rows:
                cols = r.select("td")
                if len(cols) < 2:
                    continue

                raw_player = cols[0].get_text(" ", strip=True)
                raw_player = re.sub(r"\s+(PG|SG|SF|PF|C|G|F)\s*$", "", raw_player, flags=re.I).strip()
                row_text = " | ".join([c.get_text(" ", strip=True) for c in cols]).lower()

                out_kw = ["out", "ruled out", "will not play", "inactive", "suspended"]
                is_out = any(k in row_text for k in out_kw)

                inj_list.append({
                    "NORM": norm_name(raw_player),
                    "TEAM_ABBR": t_abbr,
                    "IS_OUT": bool(is_out),
                })
    except Exception as e:
        print(f"[WARN] injuries scrape failed err={e}")

    return pd.DataFrame(inj_list)


# ---------------- probability ----------------

PROB_SCALE = float((os.environ.get("PROB_SCALE") or "12").strip())
PROB_FLOOR = float((os.environ.get("PROB_FLOOR") or "0.12").strip())
PROB_CEIL  = float((os.environ.get("PROB_CEIL") or "0.88").strip())

MODEL_FEATURE_ORDER = [
    "home_pts_sum", "away_pts_sum",
    "home_impact_mean", "away_impact_mean",
    "home_b2b", "away_b2b",
    "home_recent_w", "away_recent_w",
    "home_ts_pct", "away_ts_pct",
    "home_orb_rate", "away_orb_rate",
    "home_usage_proxy", "away_usage_proxy",
    "home_onoff_proxy", "away_onoff_proxy",
]

def fallback_cover_prob(edge_points_signed: float) -> float:
    x = abs(edge_points_signed) / max(1e-9, PROB_SCALE)
    p = 1.0 / (1.0 + math.exp(-x))
    p = max(PROB_FLOOR, min(PROB_CEIL, p))
    return float(p)


def b2b_to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    try:
        return 1 if float(v) > 0 else 0
    except Exception:
        return None


def compute_market_metrics(
    home_abbr: str,
    away_abbr: str,
    home_spread: Optional[float],
    home_odds: Optional[float],
    away_odds: Optional[float],
    base_diff: Optional[float],
    calibrator: Optional[Any],
) -> Dict[str, Optional[float]]:
    if base_diff is None or home_spread is None:
        return {"f_edge": None, "cover_prob": None, "implied_prob": None, "edge_value": None, "ev": None, "pick_team": None, "odds_used": None}

    f_edge = float(base_diff) + float(home_spread)

    if calibrator is not None:
        try:
            p = float(calibrator.predict([f_edge])[0])
            p = max(0.0, min(1.0, p))
        except Exception as e:
            print(f"[WARN] calibrator predict failed f_edge={f_edge:.4f} err={e}")
            p = fallback_cover_prob(f_edge)
    else:
        p = fallback_cover_prob(f_edge)

    pick_home = (f_edge > 0)
    odds_used = float(home_odds) if pick_home else float(away_odds)
    implied_prob = float(1.0 / odds_used) if odds_used and odds_used > 0 else None
    edge_value = float(p - implied_prob) if implied_prob is not None else None
    ev = float(p * odds_used - 1.0) if implied_prob is not None else None
    pick_team = home_abbr if pick_home else away_abbr

    return {"f_edge": f_edge, "cover_prob": p, "implied_prob": implied_prob, "edge_value": edge_value, "ev": ev, "pick_team": pick_team, "odds_used": odds_used}


def compute_data_only_metrics(
    home_abbr: str,
    away_abbr: str,
    base_diff: Optional[float],
) -> Dict[str, Optional[float]]:
    if base_diff is None:
        return {"f_edge": None, "cover_prob": None, "implied_prob": None, "edge_value": None, "ev": None, "pick_team": None, "odds_used": None}

    f_edge = float(base_diff)
    p = fallback_cover_prob(f_edge)
    pick_team = home_abbr if f_edge >= 0 else away_abbr
    return {"f_edge": f_edge, "cover_prob": p, "implied_prob": None, "edge_value": None, "ev": None, "pick_team": pick_team, "odds_used": None}


# ---------------- UPSERT ----------------

UPSERT_COLUMNS = [
    "game_id", "game_date_us", "season",
    "away_abbr", "home_abbr", "away_name", "home_name",
    "home_spread", "home_odds", "away_odds", "line_source",
    "status", "away_score", "home_score",
    "home_pts_sum", "away_pts_sum", "home_impact_mean", "away_impact_mean",
    "home_b2b", "away_b2b", "home_recent_w", "away_recent_w",
    "home_ts_pct", "away_ts_pct", "home_orb_rate", "away_orb_rate",
    "home_usage_proxy", "away_usage_proxy", "home_onoff_proxy", "away_onoff_proxy",
    "base_diff", "f_edge", "cover_prob", "implied_prob", "edge_value", "ev", "pick_team", "odds_used",
    "created_at_tw", "updated_at_tw", "game_date_tw",
]

UPSERT_SQL = """
INSERT INTO public.games (
    game_id, game_date_us, season,
    away_abbr, home_abbr, away_name, home_name,
    home_spread, home_odds, away_odds, line_source,
    status, away_score, home_score,
    home_pts_sum, away_pts_sum, home_impact_mean, away_impact_mean,
    home_b2b, away_b2b, home_recent_w, away_recent_w,
    home_ts_pct, away_ts_pct, home_orb_rate, away_orb_rate,
    home_usage_proxy, away_usage_proxy, home_onoff_proxy, away_onoff_proxy,
    base_diff, f_edge, cover_prob, implied_prob, edge_value, ev, pick_team, odds_used,
    created_at_tw, updated_at_tw, game_date_tw
) VALUES %s
ON CONFLICT (game_id)
DO UPDATE SET
    game_date_us = EXCLUDED.game_date_us,
    season = EXCLUDED.season,
    away_abbr = EXCLUDED.away_abbr,
    home_abbr = EXCLUDED.home_abbr,
    away_name = EXCLUDED.away_name,
    home_name = EXCLUDED.home_name,

    home_spread = COALESCE(EXCLUDED.home_spread, public.games.home_spread),
    home_odds   = COALESCE(EXCLUDED.home_odds,   public.games.home_odds),
    away_odds   = COALESCE(EXCLUDED.away_odds,   public.games.away_odds),
    line_source = COALESCE(EXCLUDED.line_source, public.games.line_source),

    status = EXCLUDED.status,
    away_score = EXCLUDED.away_score,
    home_score = EXCLUDED.home_score,

    home_pts_sum = COALESCE(EXCLUDED.home_pts_sum, public.games.home_pts_sum),
    away_pts_sum = COALESCE(EXCLUDED.away_pts_sum, public.games.away_pts_sum),
    home_impact_mean = COALESCE(EXCLUDED.home_impact_mean, public.games.home_impact_mean),
    away_impact_mean = COALESCE(EXCLUDED.away_impact_mean, public.games.away_impact_mean),
    home_b2b = COALESCE(EXCLUDED.home_b2b, public.games.home_b2b),
    away_b2b = COALESCE(EXCLUDED.away_b2b, public.games.away_b2b),
    home_recent_w = COALESCE(EXCLUDED.home_recent_w, public.games.home_recent_w),
    away_recent_w = COALESCE(EXCLUDED.away_recent_w, public.games.away_recent_w),
    home_ts_pct = COALESCE(EXCLUDED.home_ts_pct, public.games.home_ts_pct),
    away_ts_pct = COALESCE(EXCLUDED.away_ts_pct, public.games.away_ts_pct),
    home_orb_rate = COALESCE(EXCLUDED.home_orb_rate, public.games.home_orb_rate),
    away_orb_rate = COALESCE(EXCLUDED.away_orb_rate, public.games.away_orb_rate),
    home_usage_proxy = COALESCE(EXCLUDED.home_usage_proxy, public.games.home_usage_proxy),
    away_usage_proxy = COALESCE(EXCLUDED.away_usage_proxy, public.games.away_usage_proxy),
    home_onoff_proxy = COALESCE(EXCLUDED.home_onoff_proxy, public.games.home_onoff_proxy),
    away_onoff_proxy = COALESCE(EXCLUDED.away_onoff_proxy, public.games.away_onoff_proxy),

    base_diff = COALESCE(EXCLUDED.base_diff, public.games.base_diff),
    f_edge = COALESCE(EXCLUDED.f_edge, public.games.f_edge),
    cover_prob = COALESCE(EXCLUDED.cover_prob, public.games.cover_prob),
    implied_prob = COALESCE(EXCLUDED.implied_prob, public.games.implied_prob),
    edge_value = COALESCE(EXCLUDED.edge_value, public.games.edge_value),
    ev = COALESCE(EXCLUDED.ev, public.games.ev),
    pick_team = COALESCE(EXCLUDED.pick_team, public.games.pick_team),
    odds_used = COALESCE(EXCLUDED.odds_used, public.games.odds_used),

    updated_at_tw = EXCLUDED.updated_at_tw,
    game_date_tw = EXCLUDED.game_date_tw;
"""


def normalize_upsert_row(row: dict) -> tuple:
    r = dict(row)
    hb = b2b_to_int(r.get("home_b2b"))
    ab = b2b_to_int(r.get("away_b2b"))
    r["home_b2b"] = int(hb) if hb is not None else 0
    r["away_b2b"] = int(ab) if ab is not None else 0
    return tuple(r.get(c) for c in UPSERT_COLUMNS)


def upsert_games(rows: List[dict]) -> None:
    dedup: Dict[str, dict] = {}
    for r in rows:
        gid = str(r.get("game_id") or "").strip()
        if not gid:
            continue
        dedup[gid] = r

    if len(dedup) != len(rows):
        print(f"[WARN] dedup upsert rows={len(rows)} unique_game_id={len(dedup)}", flush=True)

    values = [normalize_upsert_row(r) for r in dedup.values()]

    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    UPSERT_SQL,
                    values,
                    page_size=200,
                )
        print(f"[INFO] db upsert ok rows={len(values)}")
    finally:
        conn.close()


# ---------------- feature build using cache ----------------

def build_team_context_from_cache(team_log_df: pd.DataFrame, game_day: dt.date) -> Dict[str, Any]:
    """
    team_log_df columns include GAME_DATE, WL
    """
    if team_log_df.empty or "GAME_DATE" not in team_log_df.columns or "WL" not in team_log_df.columns:
        return {"b2b": 0, "recent_w": 0.5}

    df = team_log_df.copy()
    # nba_api log format usually: "FEB 02, 2026"
    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"], format="%b %d, %Y", errors="coerce").dt.date
    df = df.dropna(subset=["GAME_DATE"])
    prior = df[df["GAME_DATE"] < game_day].sort_values("GAME_DATE", ascending=False)
    if prior.empty:
        return {"b2b": 0, "recent_w": 0.5}

    prev_day = game_day - dt.timedelta(days=1)
    last_game_date = prior.iloc[0]["GAME_DATE"]
    is_b2b = 1 if (last_game_date == prev_day) else 0

    last5 = prior.head(5)
    recent_w = float((last5["WL"] == "W").mean()) if len(last5) > 0 else 0.5
    return {"b2b": is_b2b, "recent_w": recent_w}


def compute_team_package(abbr: str, season: str, ps_df: pd.DataFrame, inj_df: pd.DataFrame, game_day: dt.date) -> Dict[str, Any]:
    # 1) injuries
    out_list = []
    if not inj_df.empty:
        t_inj = inj_df[inj_df["TEAM_ABBR"] == abbr]
        out_list = t_inj[t_inj["IS_OUT"]]["NORM"].tolist() if not t_inj.empty else []

    # 2) player stats from cache
    team_id = None
    if not ps_df.empty and "TEAM_ID" in ps_df.columns:
        sub = ps_df[ps_df.get("TEAM_ABBR", "") == abbr] if "TEAM_ABBR" in ps_df.columns else pd.DataFrame()
        if sub.empty:
            # fallback: find TEAM_ID by mode of that team rows
            sub2 = ps_df[ps_df["TEAM_ID"].notna()]
            # can't map abbr reliably if cache doesn't include TEAM_ABBR; so we will not block
        # For safety, treat TEAM_ID only if available
    # We'll compute using TEAM_ID rows after we load TEAM_ID mapping from ps_df by (TEAM_ABBREVIATION) if exists
    if "TEAM_ABBREVIATION" in ps_df.columns:
        rows = ps_df[ps_df["TEAM_ABBREVIATION"] == abbr]
        if not rows.empty:
            team_id = int(rows.iloc[0]["TEAM_ID"])

    active = pd.DataFrame()
    if team_id is not None and "TEAM_ID" in ps_df.columns and "NORM" in ps_df.columns:
        active = (
            ps_df[(ps_df["TEAM_ID"] == team_id) & (~ps_df["NORM"].isin(out_list))]
            .sort_values("IMPACT", ascending=False)
            .copy()
        )

    pts_sum = float(active["PTS"].sum()) if (not active.empty and "PTS" in active.columns) else 0.0
    impact_mean = float(active["IMPACT"].mean()) if (not active.empty and "IMPACT" in active.columns) else 0.0
    team_fga = float(active["FGA"].sum()) if (not active.empty and "FGA" in active.columns) else 0.0
    team_fta = float(active["FTA"].sum()) if (not active.empty and "FTA" in active.columns) else 0.0
    team_tov = float(active["TOV"].sum()) if (not active.empty and "TOV" in active.columns) else 0.0
    team_oreb = float(active["OREB"].sum()) if (not active.empty and "OREB" in active.columns) else 0.0
    team_dreb = float(active["DREB"].sum()) if (not active.empty and "DREB" in active.columns) else 0.0
    denom_ts = 2.0 * (team_fga + 0.44 * team_fta)
    ts_pct = float(pts_sum / denom_ts) if denom_ts > 0 else 0.0
    orb_rate = float(team_oreb / max(1.0, (team_oreb + team_dreb)))
    usage_proxy = float(team_fga + 0.44 * team_fta + team_tov)
    onoff_proxy = float(active["PLUS_MINUS"].mean()) if (not active.empty and "PLUS_MINUS" in active.columns) else 0.0

    # 3) team context from cached log
    log_payload = cache_get(f"team_log:{season}:{abbr}") or {}
    log_rows = log_payload.get("rows") or []
    log_df = pd.DataFrame(log_rows)
    ctx = build_team_context_from_cache(log_df, game_day=game_day)

    return {
        "pts_sum": pts_sum,
        "impact_mean": impact_mean,
        "b2b": b2b_to_int(ctx.get("b2b")) or 0,
        "recent_w": float(ctx["recent_w"]),
        "ts_pct": ts_pct,
        "orb_rate": orb_rate,
        "usage_proxy": usage_proxy,
        "onoff_proxy": onoff_proxy,
    }


def predict_margin_from_model(
    base_model: Optional[Any],
    home_pkg: Dict[str, Any],
    away_pkg: Dict[str, Any],
) -> Optional[float]:
    if base_model is None:
        return None

    feature_map = {
        "home_pts_sum": float(home_pkg.get("pts_sum") or 0.0),
        "away_pts_sum": float(away_pkg.get("pts_sum") or 0.0),
        "home_impact_mean": float(home_pkg.get("impact_mean") or 0.0),
        "away_impact_mean": float(away_pkg.get("impact_mean") or 0.0),
        "home_b2b": float(b2b_to_int(home_pkg.get("b2b")) or 0),
        "away_b2b": float(b2b_to_int(away_pkg.get("b2b")) or 0),
        "home_recent_w": float(home_pkg.get("recent_w") or 0.5),
        "away_recent_w": float(away_pkg.get("recent_w") or 0.5),
        "home_ts_pct": float(home_pkg.get("ts_pct") or 0.0),
        "away_ts_pct": float(away_pkg.get("ts_pct") or 0.0),
        "home_orb_rate": float(home_pkg.get("orb_rate") or 0.0),
        "away_orb_rate": float(away_pkg.get("orb_rate") or 0.0),
        "home_usage_proxy": float(home_pkg.get("usage_proxy") or 0.0),
        "away_usage_proxy": float(away_pkg.get("usage_proxy") or 0.0),
        "home_onoff_proxy": float(home_pkg.get("onoff_proxy") or 0.0),
        "away_onoff_proxy": float(away_pkg.get("onoff_proxy") or 0.0),
    }
    feature_row = [feature_map[k] for k in MODEL_FEATURE_ORDER]
    try:
        pred = base_model.predict([feature_row])[0]
        return float(pred)
    except Exception as e:
        print(f"[WARN] base_model predict failed err={e}", flush=True)
        return None


def main():
    log_db_env_status()
    ensure_schema()

    override = (os.environ.get("OVERRIDE_US_DATE") or "").strip()
    anchor_date_us = dt.datetime.strptime(override, "%m/%d/%Y").date() if override else us_eastern_today()

    def _int_env(name: str, default: int) -> int:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            return default
        return max(1, int(raw))

    past_days = _int_env("BACKFILL_PAST_DAYS", 1)
    future_days = _int_env("BACKFILL_FUTURE_DAYS", 1)

    past_list = [anchor_date_us - dt.timedelta(days=i) for i in range(past_days)]
    future_list = [anchor_date_us + dt.timedelta(days=i) for i in range(1, future_days)]
    date_list = past_list + future_list

    season = (os.environ.get("NBA_SEASON") or "2025-26").strip()
    FAST_MODE = (os.environ.get("FAST_MODE") or "0").strip() == "1"
    USE_ODDS = (os.environ.get("USE_ODDS") or "0").strip() == "1"

    ts_tw = now_tw_str()
    game_date_tw = today_tw_mmddyyyy()

    base_model, calibrator = load_models()
    print(f"[INFO] base_model_loaded={bool(base_model)} calibrator_loaded={bool(calibrator)} fast_mode={FAST_MODE} use_odds={USE_ODDS}")

    # odds snapshot is optional in data-only mode
    odds_map = get_odds_map() if USE_ODDS else {}

    # player stats from cache (sync itself不打nba_api)
    ps_payload = cache_get(f"player_stats:{season}") or {}
    ps_rows = ps_payload.get("rows") or []
    ps_df = pd.DataFrame(ps_rows)
    if not ps_df.empty:
        # build IMPACT + NORM once
        for c in ["GP", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV", "FGA", "FTA", "OREB", "DREB", "PLUS_MINUS"]:
            if c not in ps_df.columns:
                ps_df[c] = 0
        ps_df = ps_df[(ps_df["GP"] >= 5) & (ps_df["MIN"] >= 10)].copy()
        ps_df["IMPACT"] = (
            ps_df["PTS"]
            + ps_df["REB"] * 1.1
            + ps_df["AST"] * 1.5
            + (ps_df["STL"] + ps_df["BLK"]) * 2
            - ps_df["TOV"] * 2
        )
        ps_df["NORM"] = ps_df["PLAYER_NAME"].astype(str).map(norm_name)

    inj_df = get_injuries()

    total_rows = 0

    for d in date_list:
        is_past = d < anchor_date_us
        print(f"[INFO] ---- sync date_us={d.isoformat()} is_past={is_past} ----")

        try:
            events = fetch_espn_scoreboard(d)
            games = parse_espn_events(events, d)
            print(f"[INFO] espn games={len(games)}")
        except Exception as e:
            print(f"[ERROR] espn fetch failed for {d.isoformat()}: {e}")
            continue

        if not games:
            continue

        rows: List[dict] = []
        game_day = d

        for g in games:
            away_abbr = g["away_abbr"]
            home_abbr = g["home_abbr"]

            # odds (optional)
            sp, oh, oa, src = None, None, None, None
            if USE_ODDS:
                od = odds_map.get((away_abbr, home_abbr))
                if od:
                    sp = float(od["home_spread"])
                    oh = float(od["home_odds"])
                    oa = float(od["away_odds"])
                    src = od["line_source"]

            # features (for base model): we want them even for past games
            home_pkg = {"pts_sum": None, "impact_mean": None, "b2b": None, "recent_w": None, "ts_pct": None, "orb_rate": None, "usage_proxy": None, "onoff_proxy": None}
            away_pkg = {"pts_sum": None, "impact_mean": None, "b2b": None, "recent_w": None, "ts_pct": None, "orb_rate": None, "usage_proxy": None, "onoff_proxy": None}
            base_diff = None
            mm = {"f_edge": None, "cover_prob": None, "implied_prob": None, "edge_value": None, "ev": None, "pick_team": None, "odds_used": None}

            if not FAST_MODE:
                home_pkg = compute_team_package(home_abbr, season, ps_df, inj_df, game_day)
                away_pkg = compute_team_package(away_abbr, season, ps_df, inj_df, game_day)

                base_diff = predict_margin_from_model(base_model, home_pkg, away_pkg)

                if base_diff is None:
                    print(f"[WARN] margin_base_model unavailable; skip edge metrics game={away_abbr}@{home_abbr}", flush=True)
                    mm = {"f_edge": None, "cover_prob": None, "implied_prob": None, "edge_value": None, "ev": None, "pick_team": None, "odds_used": None}
                elif USE_ODDS and sp is not None and oh is not None and oa is not None:
                    mm = compute_market_metrics(home_abbr, away_abbr, sp, oh, oa, base_diff, calibrator)
                else:
                    mm = compute_data_only_metrics(home_abbr, away_abbr, base_diff)

            game_id = f"{d.strftime('%Y%m%d')}_{away_abbr}_{home_abbr}"

            rows.append({
                "game_id": game_id,
                "game_date_us": g["game_date_us"],
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

                # base model features
                "home_pts_sum": home_pkg.get("pts_sum"),
                "away_pts_sum": away_pkg.get("pts_sum"),
                "home_impact_mean": home_pkg.get("impact_mean"),
                "away_impact_mean": away_pkg.get("impact_mean"),
                "home_b2b": b2b_to_int(home_pkg.get("b2b")),
                "away_b2b": b2b_to_int(away_pkg.get("b2b")),
                "home_recent_w": home_pkg.get("recent_w"),
                "away_recent_w": away_pkg.get("recent_w"),
                "home_ts_pct": home_pkg.get("ts_pct"),
                "away_ts_pct": away_pkg.get("ts_pct"),
                "home_orb_rate": home_pkg.get("orb_rate"),
                "away_orb_rate": away_pkg.get("orb_rate"),
                "home_usage_proxy": home_pkg.get("usage_proxy"),
                "away_usage_proxy": away_pkg.get("usage_proxy"),
                "home_onoff_proxy": home_pkg.get("onoff_proxy"),
                "away_onoff_proxy": away_pkg.get("onoff_proxy"),

                "base_diff": base_diff,
                "f_edge": mm["f_edge"],
                "cover_prob": mm["cover_prob"],
                "implied_prob": mm["implied_prob"],
                "edge_value": mm["edge_value"],
                "ev": mm["ev"],
                "pick_team": mm["pick_team"],
                "odds_used": mm["odds_used"],

                "created_at_tw": ts_tw,
                "updated_at_tw": ts_tw,
                "game_date_tw": game_date_tw,
            })

        if rows:
            upsert_games(rows)
            total_rows += len(rows)

    print(f"[OK] sync complete rows={total_rows}")


if __name__ == "__main__":
    main()
