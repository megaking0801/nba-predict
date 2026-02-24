#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
jobs/sync_daily.py (cache-first + base model)

- ESPN scoreboard -> schedule/status/scores
- Odds API -> spreads/odds (once per run)
- nba_cache -> player_stats + team_ctx (NO nba_api calls here)
- compute features (pts_sum/impact_mean/b2b/recent_w)
- base_diff:
    if margin_base_model exists -> predict margin (pred_margin) and store as base_diff
    else -> fallback heuristic base_diff (same formula you used) using same features
- f_edge = base_diff + home_spread
- cover_prob uses cover_prob_calibrator if exists else fallback logistic
- Upsert into public.games, preserving past odds/features via COALESCE rules
"""

import os
import re
import math
import time
import json
import base64
import pickle
import unicodedata
import datetime as dt
from typing import Dict, Tuple, Optional, List, Any

import requests
import psycopg2
import psycopg2.extras
import pandas as pd
from bs4 import BeautifulSoup


# -----------------------------
# Utilities
# -----------------------------
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


def us_eastern_today() -> dt.date:
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
# Odds team map
# -----------------------------
ODDS_TEAMNAME_TO_ABBR: Dict[str, str] = {
    "atlanta hawks": "ATL", "boston celtics": "BOS", "brooklyn nets": "BKN",
    "charlotte hornets": "CHA", "chicago bulls": "CHI", "cleveland cavaliers": "CLE",
    "dallas mavericks": "DAL", "denver nuggets": "DEN", "detroit pistons": "DET",
    "golden state warriors": "GSW", "houston rockets": "HOU", "indiana pacers": "IND",
    "la clippers": "LAC", "los angeles clippers": "LAC", "la lakers": "LAL", "los angeles lakers": "LAL",
    "memphis grizzlies": "MEM", "miami heat": "MIA", "milwaukee bucks": "MIL",
    "minnesota timberwolves": "MIN", "new orleans pelicans": "NOP", "new york knicks": "NYK",
    "oklahoma city thunder": "OKC", "orlando magic": "ORL", "philadelphia 76ers": "PHI",
    "phoenix suns": "PHX", "portland trail blazers": "POR", "sacramento kings": "SAC",
    "san antonio spurs": "SAS", "toronto raptors": "TOR", "utah jazz": "UTA", "washington wizards": "WAS",
}
BOOK_KEY_ALIASES = {"pointsbet": "pointsbetus"}


# -----------------------------
# Static ABBR -> TEAM_ID (consistent with cache)
# -----------------------------
ABBR_TO_ID = {
    "ATL": 1610612737, "BOS": 1610612738, "BKN": 1610612751, "CHA": 1610612766,
    "CHI": 1610612741, "CLE": 1610612739, "DAL": 1610612742, "DEN": 1610612743,
    "DET": 1610612765, "GSW": 1610612744, "HOU": 1610612745, "IND": 1610612754,
    "LAC": 1610612746, "LAL": 1610612747, "MEM": 1610612763, "MIA": 1610612748,
    "MIL": 1610612749, "MIN": 1610612750, "NOP": 1610612740, "NYK": 1610612752,
    "OKC": 1610612760, "ORL": 1610612753, "PHI": 1610612755, "PHX": 1610612756,
    "POR": 1610612757, "SAC": 1610612758, "SAS": 1610612759, "TOR": 1610612761,
    "UTA": 1610612762, "WAS": 1610612764,
}


# -----------------------------
# DB
# -----------------------------
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
        raise RuntimeError("DB env missing: set DATABASE_URL or SUPABASE_*")

    return psycopg2.connect(
        host=host, dbname=dbname, user=user, password=password, port=int(port),
        sslmode="require",
    )


def ensure_schema():
    ddl = """
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

        base_diff DOUBLE PRECISION,
        f_edge DOUBLE PRECISION,
        cover_prob DOUBLE PRECISION,
        implied_prob DOUBLE PRECISION,
        edge_value DOUBLE PRECISION,
        ev DOUBLE PRECISION,
        pick_team TEXT,
        odds_used DOUBLE PRECISION,

        status TEXT,
        away_score INTEGER,
        home_score INTEGER,
        cover INTEGER,
        settled_at_tw TEXT,

        margin DOUBLE PRECISION,

        home_pts_sum DOUBLE PRECISION,
        away_pts_sum DOUBLE PRECISION,
        home_impact_mean DOUBLE PRECISION,
        away_impact_mean DOUBLE PRECISION,
        home_b2b INTEGER,
        away_b2b INTEGER,
        home_recent_w DOUBLE PRECISION,
        away_recent_w DOUBLE PRECISION,

        created_at_tw TEXT,
        updated_at_tw TEXT,
        game_date_tw TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_games_date_us ON public.games(game_date_us);

    CREATE TABLE IF NOT EXISTS public.model_registry (
      model_name TEXT PRIMARY KEY,
      model_version TEXT,
      payload_base64 TEXT,
      trained_rows INT,
      metrics JSONB,
      created_at_tw TEXT
    );

    CREATE TABLE IF NOT EXISTS public.nba_cache (
      cache_key TEXT PRIMARY KEY,
      season TEXT,
      payload JSONB,
      pulled_at_tw TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_nba_cache_season ON public.nba_cache(season);
    """
    alters = [
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS margin DOUBLE PRECISION",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_pts_sum DOUBLE PRECISION",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_pts_sum DOUBLE PRECISION",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_impact_mean DOUBLE PRECISION",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_impact_mean DOUBLE PRECISION",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_b2b INTEGER",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_b2b INTEGER",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_recent_w DOUBLE PRECISION",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_recent_w DOUBLE PRECISION",
    ]

    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
                for a in alters:
                    cur.execute(a)
        print("[INFO] schema ensured")
    finally:
        conn.close()


def load_models() -> Tuple[Optional[Any], Optional[Any]]:
    """
    Load margin_base_model + cover_prob_calibrator from public.model_registry.
    """
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT model_name, payload_base64 FROM public.model_registry")
            rows = cur.fetchall()

        base_model = None
        calibrator = None
        for model_name, payload_base64 in rows:
            if not payload_base64:
                continue
            obj = pickle.loads(base64.b64decode(payload_base64))
            if model_name == "margin_base_model":
                base_model = obj
            elif model_name == "cover_prob_calibrator":
                calibrator = obj
        return base_model, calibrator
    except Exception as e:
        print(f"[WARN] load_models failed: {e}")
        return None, None
    finally:
        conn.close()


def load_cache_payload(cache_key: str) -> Optional[dict]:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM public.nba_cache WHERE cache_key=%s", (cache_key,))
            row = cur.fetchone()
            if not row:
                return None
            payload = row[0]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, dict):
                return None
            return payload
    except Exception as e:
        print(f"[WARN] load_cache_payload failed key={cache_key} err={e}")
        return None
    finally:
        conn.close()


def player_stats_from_cache(season: str) -> pd.DataFrame:
    key = f"player_stats_{season}"
    payload = load_cache_payload(key)
    if not payload:
        print(f"[WARN] player_stats cache missing: {key}")
        return pd.DataFrame(columns=["PLAYER_NAME", "TEAM_ID", "GP", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV", "IMPACT", "NORM"])

    rows = payload.get("rows") or []
    if not isinstance(rows, list) or len(rows) == 0:
        print(f"[WARN] player_stats cache empty: {key}")
        return pd.DataFrame(columns=["PLAYER_NAME", "TEAM_ID", "GP", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV", "IMPACT", "NORM"])

    df = pd.DataFrame(rows)
    for c in ["TEAM_ID", "GP", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV"]:
        if c not in df.columns:
            df[c] = 0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    # Filter for stability
    df = df[(df["GP"] >= 5) & (df["MIN"] >= 10)].copy()
    df["IMPACT"] = (
        df["PTS"]
        + df["REB"] * 1.1
        + df["AST"] * 1.5
        + (df["STL"] + df["BLK"]) * 2
        - df["TOV"] * 2
    )
    df["NORM"] = df["PLAYER_NAME"].astype(str).map(norm_name)
    return df


def team_ctx_from_cache(season: str, anchor_us: dt.date) -> Dict[int, dict]:
    key_day = anchor_us.strftime("%Y%m%d")
    key = f"team_ctx_{season}_{key_day}"
    payload = load_cache_payload(key)
    if not payload:
        print(f"[WARN] team_ctx cache missing: {key}")
        return {}

    ctx = payload.get("ctx") or {}
    out: Dict[int, dict] = {}
    try:
        for k, v in ctx.items():
            out[int(k)] = {
                "b2b": bool(v.get("b2b", False)),
                "recent_w": float(v.get("recent_w", 0.5)),
            }
    except Exception:
        return {}
    return out


# -----------------------------
# ESPN scoreboard
# -----------------------------
def fetch_espn_scoreboard(date_us: dt.date) -> List[dict]:
    ymd = date_us.strftime("%Y%m%d")
    candidates = [
        ("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", {"dates": ymd, "limit": 300}),
        ("https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", {"dates": ymd, "limit": 300}),
    ]
    last_err = None
    for url, params in candidates:
        try:
            r = requests.get(url, params=params, timeout=25)
            if r.status_code == 200:
                data = r.json()
                events = data.get("events") or []
                print(f"[INFO] espn ok games={len(events)} url={url}")
                return events
            last_err = Exception(f"status={r.status_code} url={r.url}")
        except Exception as e:
            last_err = e
    raise RuntimeError(f"espn failed: {last_err}")


def parse_espn_events(events: List[dict], date_us: dt.date) -> List[dict]:
    out: List[dict] = []
    game_date_str = date_us.strftime("%m/%d/%Y")

    for ev in events:
        try:
            comps = ev.get("competitions") or []
            if not comps:
                continue
            comp = comps[0]
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
                except Exception:
                    home_score, away_score = None, None

            out.append({
                "game_date_us": game_date_str,
                "home_abbr": home_abbr,
                "away_abbr": away_abbr,
                "home_name": home_team.get("displayName") or home_abbr,
                "away_name": away_team.get("displayName") or away_abbr,
                "home_score": home_score,
                "away_score": away_score,
                "status": status,
            })
        except Exception:
            continue

    return out


# -----------------------------
# Odds API
# -----------------------------
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
        except Exception:
            continue

    print(f"[INFO] odds mapped={len(out)}")
    return out


# -----------------------------
# Injuries (ESPN best-effort)
# -----------------------------
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

                inj_list.append({"NORM": norm_name(raw_player), "TEAM_ABBR": t_abbr, "IS_OUT": bool(is_out)})
    except Exception as e:
        print(f"[WARN] injuries failed: {e}")

    return pd.DataFrame(inj_list)


# -----------------------------
# Feature builder
# -----------------------------
def build_team_features(team_abbr: str, ps_db: pd.DataFrame, inj_db: pd.DataFrame, ctx_db: Dict[int, dict]) -> Dict[str, Any]:
    tid = ABBR_TO_ID.get(team_abbr)
    if not tid:
        return {
            "pts_sum": None,
            "impact_mean": None,
            "b2b": 0,
            "recent_w": 0.5,
        }

    ctx = ctx_db.get(tid, {"b2b": False, "recent_w": 0.5})
    b2b = 1 if bool(ctx.get("b2b", False)) else 0
    recent_w = float(ctx.get("recent_w", 0.5))

    # injuries out list
    out_list: List[str] = []
    if inj_db is not None and not inj_db.empty:
        t_inj = inj_db[inj_db["TEAM_ABBR"] == team_abbr]
        if not t_inj.empty:
            out_list = t_inj[t_inj["IS_OUT"]]["NORM"].tolist()

    # active players
    pts_sum = 0.0
    impact_mean = 0.0
    if ps_db is not None and not ps_db.empty and "TEAM_ID" in ps_db.columns and "NORM" in ps_db.columns:
        active = ps_db[(ps_db["TEAM_ID"] == tid) & (~ps_db["NORM"].isin(out_list))].copy()
        if not active.empty:
            pts_sum = float(active["PTS"].sum()) if "PTS" in active.columns else 0.0
            impact_mean = float(active["IMPACT"].mean()) if "IMPACT" in active.columns else 0.0

    return {
        "pts_sum": float(pts_sum),
        "impact_mean": float(impact_mean),
        "b2b": int(b2b),
        "recent_w": float(recent_w),
    }


def fallback_base_diff_from_features(h: Dict[str, Any], a: Dict[str, Any]) -> Optional[float]:
    # Same heuristic shape as before; now expressed from stored features.
    if h["pts_sum"] is None or a["pts_sum"] is None:
        return None
    home_adv = 2.5
    b2b_v = (-2.5 if h["b2b"] else 0.0) - (-2.5 if a["b2b"] else 0.0)
    recent_v = (h["recent_w"] - a["recent_w"]) * 5.0
    base_diff = (h["pts_sum"] - a["pts_sum"]) * 0.09 + (h["impact_mean"] - a["impact_mean"]) * 3.8 + home_adv + b2b_v + recent_v
    return float(base_diff)


# -----------------------------
# Probability + EV
# -----------------------------
PROB_SCALE = float((os.environ.get("PROB_SCALE") or "12").strip())
PROB_FLOOR = float((os.environ.get("PROB_FLOOR") or "0.12").strip())
PROB_CEIL  = float((os.environ.get("PROB_CEIL") or "0.88").strip())

def fallback_cover_prob(edge_points_signed: float) -> float:
    x = abs(edge_points_signed) / max(1e-9, PROB_SCALE)
    p = 1.0 / (1.0 + math.exp(-x))
    p = max(PROB_FLOOR, min(PROB_CEIL, p))
    return float(p)

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
        except Exception:
            p = fallback_cover_prob(f_edge)
    else:
        p = fallback_cover_prob(f_edge)

    pick_home = (f_edge > 0)
    odds_used = float(home_odds) if pick_home else float(away_odds)

    implied_prob = float(1.0 / odds_used) if odds_used and odds_used > 0 else None
    edge_value = float(p - implied_prob) if implied_prob is not None else None
    ev = float(p * odds_used - 1.0) if implied_prob is not None else None

    pick_team = home_abbr if pick_home else away_abbr

    return {
        "f_edge": float(f_edge),
        "cover_prob": float(p),
        "implied_prob": implied_prob,
        "edge_value": edge_value,
        "ev": ev,
        "pick_team": pick_team,
        "odds_used": float(odds_used),
    }


# -----------------------------
# UPSERT
# -----------------------------
UPSERT_SQL = """
INSERT INTO public.games (
    game_id, game_date_us, season,
    away_abbr, home_abbr, away_name, home_name,

    home_spread, home_odds, away_odds, line_source,

    base_diff, f_edge, cover_prob, implied_prob, edge_value, ev, pick_team, odds_used,

    status, away_score, home_score,

    home_pts_sum, away_pts_sum, home_impact_mean, away_impact_mean,
    home_b2b, away_b2b, home_recent_w, away_recent_w,

    created_at_tw, updated_at_tw, game_date_tw
) VALUES (
    %(game_id)s, %(game_date_us)s, %(season)s,
    %(away_abbr)s, %(home_abbr)s, %(away_name)s, %(home_name)s,

    %(home_spread)s, %(home_odds)s, %(away_odds)s, %(line_source)s,

    %(base_diff)s, %(f_edge)s, %(cover_prob)s, %(implied_prob)s, %(edge_value)s, %(ev)s, %(pick_team)s, %(odds_used)s,

    %(status)s, %(away_score)s, %(home_score)s,

    %(home_pts_sum)s, %(away_pts_sum)s, %(home_impact_mean)s, %(away_impact_mean)s,
    %(home_b2b)s, %(away_b2b)s, %(home_recent_w)s, %(away_recent_w)s,

    %(created_at_tw)s, %(updated_at_tw)s, %(game_date_tw)s
)
ON CONFLICT (game_id)
DO UPDATE SET
    game_date_us = EXCLUDED.game_date_us,
    season       = EXCLUDED.season,
    away_abbr    = EXCLUDED.away_abbr,
    home_abbr    = EXCLUDED.home_abbr,
    away_name    = EXCLUDED.away_name,
    home_name    = EXCLUDED.home_name,

    home_spread  = COALESCE(EXCLUDED.home_spread, public.games.home_spread),
    home_odds    = COALESCE(EXCLUDED.home_odds,   public.games.home_odds),
    away_odds    = COALESCE(EXCLUDED.away_odds,   public.games.away_odds),
    line_source  = COALESCE(EXCLUDED.line_source, public.games.line_source),

    base_diff    = COALESCE(EXCLUDED.base_diff,   public.games.base_diff),
    f_edge       = COALESCE(EXCLUDED.f_edge,      public.games.f_edge),
    cover_prob   = COALESCE(EXCLUDED.cover_prob,  public.games.cover_prob),
    implied_prob = COALESCE(EXCLUDED.implied_prob,public.games.implied_prob),
    edge_value   = COALESCE(EXCLUDED.edge_value,  public.games.edge_value),
    ev           = COALESCE(EXCLUDED.ev,          public.games.ev),
    pick_team    = COALESCE(EXCLUDED.pick_team,   public.games.pick_team),
    odds_used    = COALESCE(EXCLUDED.odds_used,   public.games.odds_used),

    home_pts_sum     = COALESCE(EXCLUDED.home_pts_sum, public.games.home_pts_sum),
    away_pts_sum     = COALESCE(EXCLUDED.away_pts_sum, public.games.away_pts_sum),
    home_impact_mean = COALESCE(EXCLUDED.home_impact_mean, public.games.home_impact_mean),
    away_impact_mean = COALESCE(EXCLUDED.away_impact_mean, public.games.away_impact_mean),
    home_b2b         = COALESCE(EXCLUDED.home_b2b, public.games.home_b2b),
    away_b2b         = COALESCE(EXCLUDED.away_b2b, public.games.away_b2b),
    home_recent_w    = COALESCE(EXCLUDED.home_recent_w, public.games.home_recent_w),
    away_recent_w    = COALESCE(EXCLUDED.away_recent_w, public.games.away_recent_w),

    status       = EXCLUDED.status,
    away_score   = EXCLUDED.away_score,
    home_score   = EXCLUDED.home_score,

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
                psycopg2.extras.execute_batch(cur, UPSERT_SQL, rows, page_size=200)
        print(f"[INFO] db upsert ok rows={len(rows)}")
    finally:
        conn.close()


# -----------------------------
# Main
# -----------------------------
def main():
    FAST_MODE = (os.environ.get("FAST_MODE") or "").strip() == "1"
    ensure_schema()

    override = (os.environ.get("OVERRIDE_US_DATE") or "").strip()
    if override:
        anchor_us = dt.datetime.strptime(override, "%m/%d/%Y").date()
    else:
        anchor_us = us_eastern_today()

    def _int_env(name: str, default: int) -> int:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            return default
        return max(1, int(raw))

    past_days = _int_env("BACKFILL_PAST_DAYS", 7)
    future_days = _int_env("BACKFILL_FUTURE_DAYS", 7)

    season = (os.environ.get("NBA_SEASON") or "2025-26").strip()

    ts_tw = now_tw_str()
    game_date_tw = today_tw_mmddyyyy()

    base_model, calibrator = load_models()
    print(f"[INFO] base_model(margin)={bool(base_model)} calibrator={bool(calibrator)} FAST_MODE={FAST_MODE}")

    t0 = time.time()
    odds_map = get_odds_map()
    print(f"[T] odds_map took {round(time.time()-t0,2)}s")

    if FAST_MODE:
        ps_db = pd.DataFrame()
        inj_db = pd.DataFrame()
        ctx_db: Dict[int, dict] = {}
    else:
        ps_db = player_stats_from_cache(season)
        ctx_db = team_ctx_from_cache(season, anchor_us)
        inj_db = get_injuries()

    date_list = [anchor_us - dt.timedelta(days=i) for i in range(past_days)] + \
                [anchor_us + dt.timedelta(days=i) for i in range(1, future_days)]

    total_rows = 0

    for d in date_list:
        is_past = d < anchor_us
        print(f"[INFO] ---- sync date_us={d.isoformat()} is_past={is_past} ----")

        try:
            events = fetch_espn_scoreboard(d)
            games = parse_espn_events(events, d)
            print(f"[INFO] espn games={len(games)}")
        except Exception as e:
            print(f"[ERROR] espn fetch failed date={d.isoformat()} err={e}")
            continue

        if not games:
            continue

        rows: List[dict] = []

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
                    sp, oh, oa, src = None, None, None, None
                else:
                    sp, oh, oa, src = 0.0, 1.90, 1.90, "Fallback ⚠️"

            # compute features + base_diff only when meaningful (avoid burning on past without spread)
            should_compute = (not FAST_MODE) and ((not is_past) or (sp is not None))

            home_feats = {"pts_sum": None, "impact_mean": None, "b2b": 0, "recent_w": 0.5}
            away_feats = {"pts_sum": None, "impact_mean": None, "b2b": 0, "recent_w": 0.5}

            base_diff = None
            if should_compute:
                home_feats = build_team_features(home_abbr, ps_db, inj_db, ctx_db)
                away_feats = build_team_features(away_abbr, ps_db, inj_db, ctx_db)

                # If we have trained margin model, predict margin -> base_diff
                if base_model is not None:
                    try:
                        X = [[
                            home_feats["pts_sum"], away_feats["pts_sum"],
                            home_feats["impact_mean"], away_feats["impact_mean"],
                            home_feats["b2b"], away_feats["b2b"],
                            home_feats["recent_w"], away_feats["recent_w"],
                        ]]
                        pred = float(base_model.predict(X)[0])
                        base_diff = pred
                    except Exception as e:
                        print(f"[WARN] base_model predict failed game={away_abbr}@{home_abbr} err={e}")
                        base_diff = fallback_base_diff_from_features(home_feats, away_feats)
                else:
                    base_diff = fallback_base_diff_from_features(home_feats, away_feats)

            mm = compute_market_metrics(
                home_abbr=home_abbr,
                away_abbr=away_abbr,
                home_spread=sp,
                home_odds=oh,
                away_odds=oa,
                base_diff=base_diff,
                calibrator=calibrator,
            ) if should_compute else {"f_edge": None, "cover_prob": None, "implied_prob": None, "edge_value": None, "ev": None, "pick_team": None, "odds_used": None}

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

                "base_diff": base_diff,
                "f_edge": mm["f_edge"],
                "cover_prob": mm["cover_prob"],
                "implied_prob": mm["implied_prob"],
                "edge_value": mm["edge_value"],
                "ev": mm["ev"],
                "pick_team": mm["pick_team"],
                "odds_used": mm["odds_used"],

                "status": g["status"],
                "away_score": g["away_score"],
                "home_score": g["home_score"],

                "home_pts_sum": home_feats["pts_sum"],
                "away_pts_sum": away_feats["pts_sum"],
                "home_impact_mean": home_feats["impact_mean"],
                "away_impact_mean": away_feats["impact_mean"],
                "home_b2b": home_feats["b2b"],
                "away_b2b": away_feats["b2b"],
                "home_recent_w": home_feats["recent_w"],
                "away_recent_w": away_feats["recent_w"],

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
