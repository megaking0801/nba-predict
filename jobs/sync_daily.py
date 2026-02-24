#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
jobs/sync_daily.py (Strategy B: cache-first, NO nba_api in sync)

- ESPN scoreboard -> games list (schedule/status/scores)
- Odds API spreads (once per run) -> odds map
- Load nba_api cache from public.nba_cache:
  - player_stats_{season}
  - team_gamelog_{season}_{team_id}
- ESPN injuries -> OUT list
- compute base_diff / f_edge / cover_prob / EV metrics
- Upsert into Supabase/Postgres public.games

FAST_MODE=1:
- skip cache + injuries + base_diff (only schedule + odds)
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


# =========================================================
# Utilities
# =========================================================

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


# =========================================================
# Team mapping (Odds API -> Abbr)
# =========================================================

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


# =========================================================
# ESPN scoreboard
# =========================================================

def fetch_espn_scoreboard(date_us: dt.date) -> List[dict]:
    ymd = date_us.strftime("%Y%m%d")
    candidates = [
        ("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", {"dates": ymd, "limit": 300}),
        ("https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", {"dates": ymd, "limit": 300}),
    ]

    last_err: Optional[Exception] = None
    for url, params in candidates:
        try:
            r = requests.get(url, params=params, timeout=25)
            if r.status_code == 200:
                data = r.json()
                events = data.get("events") or []
                print(f"[INFO] espn scoreboard ok games={len(events)} url={url}")
                return events
            print(f"[WARN] espn scoreboard status={r.status_code} url={url} body={r.text[:160]}")
            last_err = Exception(f"status={r.status_code} url={r.url}")
        except Exception as e:
            print(f"[WARN] espn scoreboard error url={url} err={e}")
            last_err = e

    raise RuntimeError(f"espn scoreboard failed after fallbacks: {last_err}")


def parse_espn_events(events: List[dict], date_us: dt.date) -> List[dict]:
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


# =========================================================
# Odds API spreads
# =========================================================

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


# =========================================================
# DB connection + schema
# =========================================================

def db_connect():
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
        raise RuntimeError("DB env missing: set DATABASE_URL or SUPABASE_HOST/DB/USER/PASSWORD/PORT")

    return psycopg2.connect(
        host=host,
        dbname=dbname,
        user=user,
        password=password,
        port=int(port),
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

        created_at_tw TEXT,
        updated_at_tw TEXT,
        game_date_tw TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_games_date_us ON public.games (game_date_us);

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
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
        print("[INFO] schema ensured")
    finally:
        conn.close()


def load_models() -> Tuple[Optional[Any], Optional[Any]]:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT model_name, payload_base64 FROM public.model_registry""")
            rows = cur.fetchall()

        base_model = None
        calibrator = None
        for model_name, payload_base64 in rows:
            if not payload_base64:
                continue
            obj = pickle.loads(base64.b64decode(payload_base64))
            if model_name == "cover_base_model":
                base_model = obj
            elif model_name == "cover_prob_calibrator":
                calibrator = obj

        return base_model, calibrator
    except Exception:
        return None, None
    finally:
        conn.close()


# =========================================================
# Cache loaders
# =========================================================

def load_cache_payload(cache_key: str) -> Optional[dict]:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload, pulled_at_tw FROM public.nba_cache WHERE cache_key=%s",
                (cache_key,),
            )
            row = cur.fetchone()
            if not row:
                return None
            payload, pulled_at_tw = row
            # payload is JSONB -> psycopg2 returns dict already in many setups, but be safe:
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
        print(f"[WARN] player stats cache missing: {key}")
        return pd.DataFrame(columns=["PLAYER_NAME", "TEAM_ID", "PTS", "IMPACT", "NORM", "GP", "MIN"])

    try:
        rs0 = payload["resultSets"][0]
        df = pd.DataFrame(rs0["rowSet"], columns=rs0["headers"])
    except Exception:
        print(f"[WARN] player stats cache malformed: {key}")
        return pd.DataFrame(columns=["PLAYER_NAME", "TEAM_ID", "PTS", "IMPACT", "NORM", "GP", "MIN"])

    if df.empty or "TEAM_ID" not in df.columns or "PLAYER_NAME" not in df.columns:
        return pd.DataFrame(columns=["PLAYER_NAME", "TEAM_ID", "PTS", "IMPACT", "NORM", "GP", "MIN"])

    for c in ["GP", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV"]:
        if c not in df.columns:
            df[c] = 0

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


def teamlog_from_cache(season: str, team_id: int) -> pd.DataFrame:
    key = f"team_gamelog_{season}_{team_id}"
    payload = load_cache_payload(key)
    if not payload:
        return pd.DataFrame()
    try:
        rs0 = payload["resultSets"][0]
        df = pd.DataFrame(rs0["rowSet"], columns=rs0["headers"])
        return df
    except Exception:
        return pd.DataFrame()


# =========================================================
# Injuries (ESPN)
# =========================================================

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
        print(f"[WARN] injuries fetch/parse failed: {e}")

    return pd.DataFrame(inj_list)


# =========================================================
# Team context from cached team logs
# =========================================================

def get_team_context_from_cache(team_ids: List[int], game_date_us: str, season: str) -> Dict[int, dict]:
    ctx: Dict[int, dict] = {}
    game_day = dt.datetime.strptime(game_date_us, "%m/%d/%Y").date()
    prev_day = game_day - dt.timedelta(days=1)

    for tid in team_ids:
        log = teamlog_from_cache(season=season, team_id=tid)
        is_b2b, recent_w = False, 0.5

        if not log.empty and "GAME_DATE" in log.columns and "WL" in log.columns:
            log = log.head(15).copy()
            log["GAME_DATE"] = pd.to_datetime(
                log["GAME_DATE"],
                format="%b %d, %Y",
                errors="coerce"
            ).dt.date
            log = log.dropna(subset=["GAME_DATE"])

            prior = log[log["GAME_DATE"] < game_day].sort_values("GAME_DATE", ascending=False)
            if not prior.empty:
                last_game_date = prior.iloc[0]["GAME_DATE"]
                is_b2b = (last_game_date == prev_day)
                last5 = prior.head(5)
                if len(last5) > 0:
                    recent_w = (last5["WL"] == "W").mean()

        ctx[tid] = {"b2b": bool(is_b2b), "recent_w": float(recent_w)}

    return ctx


# =========================================================
# base_diff
# =========================================================

def compute_base_diff(
    home_abbr: str,
    away_abbr: str,
    ps_db: pd.DataFrame,
    inj_db: pd.DataFrame,
    ctx_db: Dict[int, dict],
) -> Optional[float]:
    hid = ABBR_TO_ID.get(home_abbr)
    aid = ABBR_TO_ID.get(away_abbr)
    if not hid or not aid:
        return None

    def build_pkg(tid: int, abbr: str):
        ctx = ctx_db.get(tid, {"b2b": False, "recent_w": 0.5})

        t_inj = inj_db[inj_db["TEAM_ABBR"] == abbr] if (inj_db is not None and not inj_db.empty) else pd.DataFrame()
        out_list = t_inj[t_inj["IS_OUT"]]["NORM"].tolist() if not t_inj.empty else []

        if ps_db is not None and not ps_db.empty and "TEAM_ID" in ps_db.columns and "NORM" in ps_db.columns:
            active = (
                ps_db[(ps_db["TEAM_ID"] == tid) & (~ps_db["NORM"].isin(out_list))]
                .sort_values("IMPACT", ascending=False)
                .copy()
            )
        else:
            active = pd.DataFrame()

        pts_sum = float(active["PTS"].sum()) if (not active.empty and "PTS" in active.columns) else 0.0
        impact_mean = float(active["IMPACT"].mean()) if (not active.empty and "IMPACT" in active.columns) else 0.0

        return {
            "pts": pts_sum,
            "impact": impact_mean,
            "b2b": bool(ctx["b2b"]),
            "recent_w": float(ctx["recent_w"]),
        }

    h_p = build_pkg(hid, home_abbr)
    a_p = build_pkg(aid, away_abbr)

    b2b_v = (-2.5 if h_p["b2b"] else 0) - (-2.5 if a_p["b2b"] else 0)
    recent_v = (h_p["recent_w"] - a_p["recent_w"]) * 5

    base_diff = (h_p["pts"] - a_p["pts"]) * 0.09 + (h_p["impact"] - a_p["impact"]) * 3.8 + 2.5 + b2b_v + recent_v
    return float(base_diff)


# =========================================================
# Probability + EV
# =========================================================

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
        return {
            "f_edge": None,
            "cover_prob": None,
            "implied_prob": None,
            "edge_value": None,
            "ev": None,
            "pick_team": None,
            "odds_used": None,
        }

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
        "odds_used": odds_used,
    }


# =========================================================
# UPSERT
# =========================================================

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

    base_diff,
    f_edge,
    cover_prob,
    implied_prob,
    edge_value,
    ev,
    pick_team,
    odds_used,

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

    %(base_diff)s,
    %(f_edge)s,
    %(cover_prob)s,
    %(implied_prob)s,
    %(edge_value)s,
    %(ev)s,
    %(pick_team)s,
    %(odds_used)s,

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

    home_spread   = COALESCE(EXCLUDED.home_spread, public.games.home_spread),
    home_odds     = COALESCE(EXCLUDED.home_odds,   public.games.home_odds),
    away_odds     = COALESCE(EXCLUDED.away_odds,   public.games.away_odds),
    line_source   = COALESCE(EXCLUDED.line_source, public.games.line_source),

    base_diff     = COALESCE(EXCLUDED.base_diff,   public.games.base_diff),
    f_edge        = COALESCE(EXCLUDED.f_edge,      public.games.f_edge),
    cover_prob    = COALESCE(EXCLUDED.cover_prob,  public.games.cover_prob),
    implied_prob  = COALESCE(EXCLUDED.implied_prob,public.games.implied_prob),
    edge_value    = COALESCE(EXCLUDED.edge_value,  public.games.edge_value),
    ev            = COALESCE(EXCLUDED.ev,          public.games.ev),
    pick_team     = COALESCE(EXCLUDED.pick_team,   public.games.pick_team),
    odds_used     = COALESCE(EXCLUDED.odds_used,   public.games.odds_used),

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
                psycopg2.extras.execute_batch(cur, UPSERT_SQL, rows, page_size=200)
        print(f"[INFO] db upsert ok rows={len(rows)}")
    finally:
        conn.close()


# =========================================================
# Main
# =========================================================

def main():
    FAST_MODE = (os.environ.get("FAST_MODE") or "").strip() == "1"
    print(f"[INFO] FAST_MODE={FAST_MODE}")
    print(f"[INFO] ODDS_API_KEY_present={bool((os.environ.get('ODDS_API_KEY') or '').strip())}")

    ensure_schema()

    override = (os.environ.get("OVERRIDE_US_DATE") or "").strip()
    if override:
        try:
            anchor_date_us = dt.datetime.strptime(override, "%m/%d/%Y").date()
        except ValueError:
            raise RuntimeError("OVERRIDE_US_DATE must be MM/DD/YYYY")
    else:
        anchor_date_us = us_eastern_today()

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
    ts_tw = now_tw_str()
    game_date_tw = today_tw_mmddyyyy()

    base_model, calibrator = load_models()
    print(f"[INFO] base_model={bool(base_model)} calibrator={bool(calibrator)}")

    # odds snapshot
    t0 = time.time()
    odds_map = get_odds_map()
    print(f"[T] odds_map ready took={round(time.time()-t0,2)}s")

    if FAST_MODE:
        ps_db = pd.DataFrame()
        inj_db = pd.DataFrame()
        print("[INFO] FAST_MODE -> skip cache/injuries/base_diff")
    else:
        t0 = time.time()
        ps_db = player_stats_from_cache(season)
        print(f"[T] player_stats(cache) took={round(time.time()-t0,2)}s rows={len(ps_db)}")

        t0 = time.time()
        inj_db = get_injuries()
        print(f"[T] injuries took={round(time.time()-t0,2)}s rows={len(inj_db)}")

    total_rows = 0

    for d in date_list:
        is_past = d < anchor_date_us
        print(f"[INFO] ---- sync date_us={d.isoformat()} is_past={is_past} ----")

        t0 = time.time()
        try:
            events = fetch_espn_scoreboard(d)
            games = parse_espn_events(events, d)
            print(f"[INFO] espn games={len(games)} took={round(time.time()-t0,2)}s")
        except Exception as e:
            print(f"[ERROR] espn fetch failed for {d.isoformat()}: {e}")
            continue

        if not games:
            continue

        # ctx only needed when computing base_diff
        ctx_db: Dict[int, dict] = {}
        if not FAST_MODE:
            team_ids: List[int] = []
            for gg in games:
                hid = ABBR_TO_ID.get(gg["home_abbr"])
                aid = ABBR_TO_ID.get(gg["away_abbr"])
                if hid:
                    team_ids.append(hid)
                if aid:
                    team_ids.append(aid)
            team_ids = sorted(set(team_ids))
            if team_ids:
                t0 = time.time()
                ctx_db = get_team_context_from_cache(team_ids, game_date_us=d.strftime("%m/%d/%Y"), season=season)
                print(f"[INFO] ctx(cache) teams={len(team_ids)} got={len(ctx_db)} took={round(time.time()-t0,2)}s")

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

            should_compute = (not FAST_MODE) and ((not is_past) or (sp is not None))

            base_diff = None
            mm = {
                "f_edge": None,
                "cover_prob": None,
                "implied_prob": None,
                "edge_value": None,
                "ev": None,
                "pick_team": None,
                "odds_used": None,
            }

            if should_compute:
                base_diff = compute_base_diff(
                    home_abbr=home_abbr,
                    away_abbr=away_abbr,
                    ps_db=ps_db,
                    inj_db=inj_db,
                    ctx_db=ctx_db,
                )
                mm = compute_market_metrics(
                    home_abbr=home_abbr,
                    away_abbr=away_abbr,
                    home_spread=sp,
                    home_odds=oh,
                    away_odds=oa,
                    base_diff=base_diff,
                    calibrator=calibrator,
                )

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

                "created_at_tw": ts_tw,
                "updated_at_tw": ts_tw,
                "game_date_tw": game_date_tw,
            })

        if rows:
            t0 = time.time()
            upsert_games(rows)
            total_rows += len(rows)
            print(f"[INFO] upsert rows={len(rows)} took={round(time.time()-t0,2)}s")

    print(f"[OK] sync complete rows={total_rows}")


if __name__ == "__main__":
    main()
