import streamlit as st
from nba_api.stats.endpoints import scoreboardv2, leaguedashplayerstats, teamgamelog, playercareerstats
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests, re, unicodedata, time, math
import os
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# ===== Supabase(Postgres) driver =====
import psycopg2
from psycopg2.extras import execute_values

# ===== NEW: load models from DB =====
import base64, pickle, json
from sklearn.isotonic import IsotonicRegression  # keep import so pickle loads

# =========================================================
# 1) 核心配置（保留原 UI；強化邏輯與穩定性）
# =========================================================
warnings.filterwarnings("ignore")

tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")

TEAM_MAP = {
    "ATL": ["Atlanta Hawks", "老鷹"], "BKN": ["Brooklyn Nets", "籃網"], "BOS": ["Boston Celtics", "塞爾提克"],
    "CHA": ["Charlotte Hornets", "黃蜂"], "CHI": ["Chicago Bulls", "公牛"], "CLE": ["Cleveland Cavaliers", "騎士"],
    "DAL": ["Dallas Mavericks", "獨行俠"], "DEN": ["Denver Nuggets", "金塊"], "DET": ["Detroit Pistons", "活塞"],
    "GSW": ["Golden State Warriors", "勇士"], "HOU": ["Houston Rockets", "火箭"], "IND": ["Indiana Pacers", "溜馬"],
    "LAC": ["LA Clippers", "快艇"], "LAL": ["Los Angeles Lakers", "湖人"], "MEM": ["Memphis Grizzlies", "灰熊"],
    "MIA": ["Miami Heat", "熱火"], "MIL": ["Milwaukee Bucks", "公鹿"], "MIN": ["Minnesota Timberwolves", "灰狼"],
    "NOP": ["New Orleans Pelicans", "鵜鶘"], "NYK": ["New York Knicks", "尼克"], "OKC": ["Oklahoma City Thunder", "雷霆"],
    "ORL": ["Orlando Magic", "魔術"], "PHI": ["Philadelphia 76ers", "76人"], "PHX": ["Phoenix Suns", "太陽"],
    "POR": ["Portland Trail Blazers", "拓荒者"], "SAC": ["Sacramento Kings", "國王"], "SAS": ["San Antonio Spurs", "馬刺"],
    "TOR": ["Toronto Raptors", "暴龍"], "UTA": ["Utah Jazz", "爵士"], "WAS": ["Washington Wizards", "巫師"],
}
TEAM_NAME_CH = {k: v[1] for k, v in TEAM_MAP.items()}

@st.cache_resource
def _load_all_teams():
    return teams.get_teams()

ALL_TEAMS = _load_all_teams()
VALID_TEAM_IDS = [t["id"] for t in ALL_TEAMS]
ID_MAP = {t["id"]: t["abbreviation"] for t in ALL_TEAMS}

# Odds API 端常見隊名 → 我們的縮寫（盡量涵蓋變形）
ODDS_TEAMNAME_TO_ABBR = {
    "atlanta hawks": "ATL",
    "brooklyn nets": "BKN",
    "boston celtics": "BOS",
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

# =========================================================
# 2) 工具：名字正規化 + endpoint 安全抓取（含簡單重試）
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

def fetch_safe_df(endpoint, retries: int = 2, sleep_s: float = 0.6, **kwargs) -> pd.DataFrame:
    for attempt in range(retries + 1):
        try:
            r = endpoint(**kwargs).get_dict()
            res = r["resultSets"][0]
            return pd.DataFrame(res["rowSet"], columns=res["headers"])
        except Exception:
            if attempt < retries:
                time.sleep(sleep_s * (attempt + 1))
            else:
                return pd.DataFrame()

# =========================================================
# 2.1) fallback 機率映射（更保守：12%~88%）
# =========================================================
PROB_SCALE = 12.0
PROB_FLOOR = 0.12
PROB_CEIL  = 0.88

def calc_cover_prob(edge_points: float) -> float:
    x = abs(edge_points) / PROB_SCALE
    p = 1.0 / (1.0 + math.exp(-x))
    if p < PROB_FLOOR:
        p = PROB_FLOOR
    if p > PROB_CEIL:
        p = PROB_CEIL
    return p

# =========================================================
# NEW) Supabase(Postgres) DB：連線 / 建表 / upsert / bulk / 結算
# =========================================================
def pg_conn():
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not db_url and "DATABASE_URL" in st.secrets:
        db_url = str(st.secrets["DATABASE_URL"]).strip()
    if "DATABASE_URL" in st.secrets:
        db_url = db_url or str(st.secrets["DATABASE_URL"]).strip()
    if db_url:
        return psycopg2.connect(db_url, connect_timeout=8)

    host = st.secrets["SUPABASE_HOST"]
    db = str(st.secrets["SUPABASE_DB"]) if "SUPABASE_DB" in st.secrets else "postgres"
    user = st.secrets["SUPABASE_USER"]
    pw = st.secrets["SUPABASE_PASSWORD"]
    port = int(st.secrets["SUPABASE_PORT"]) if "SUPABASE_PORT" in st.secrets else 5432

    return psycopg2.connect(
        host=host,
        dbname=db,
        user=user,
        password=pw,
        port=port,
        connect_timeout=8,
        sslmode="require",
    )

def nba_cache_get(cache_key: str) -> dict | None:
    """從 nba_cache 表讀取 cache_nba.py 寫入的快取，找不到回傳 None。"""
    try:
        conn = pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload_json FROM public.nba_cache WHERE cache_key = %s LIMIT 1",
                    (cache_key,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row and row[0]:
            return json.loads(row[0])
    except Exception:
        pass
    return None

def norm_team_abbr(a: str) -> str:
    x = str(a or "").strip().upper()
    if x in ("GS", "GSW"):
        return "GSW"
    if x in ("NO", "NOP"):
        return "NOP"
    if x in ("NY", "NYK"):
        return "NYK"
    if x in ("SA", "SAS"):
        return "SAS"
    if x in ("UTAH", "UTA"):
        return "UTA"
    return x


def load_existing_game_id_map(game_date_us: str, season: str) -> dict:
    """Map (away_abbr, home_abbr) -> existing game_id in DB for that date/season."""
    conn = pg_conn()
    out = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT game_id, away_abbr, home_abbr
                FROM games
                WHERE season=%s AND game_date_us=%s
                """,
                (season, game_date_us),
            )
            rows = cur.fetchall()

        for gid, away, home in rows:
            key = (norm_team_abbr(away), norm_team_abbr(home))
            # prefer numeric/event-like ids when multiple rows exist for same matchup/date
            gid_txt = str(gid)
            prev = out.get(key)
            if prev is None:
                out[key] = gid_txt
            elif (not str(prev).isdigit()) and gid_txt.isdigit():
                out[key] = gid_txt
        return out
    finally:
        conn.close()

def ensure_model_registry():
    sql = """
    CREATE TABLE IF NOT EXISTS model_registry (
      model_name TEXT PRIMARY KEY,
      model_version TEXT,
      payload_base64 TEXT,
      trained_rows INT,
      metrics JSONB,
      created_at_tw TEXT
    );
    """
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        conn.close()

def db_init():
    """
    你的原本 games table + ✅ 新增 ML 需要欄位（ALTER IF NOT EXISTS）
    """
    sql = """
    CREATE TABLE IF NOT EXISTS games (
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

        status TEXT,            -- scheduled/in_progress/final
        away_score INTEGER,
        home_score INTEGER,
        cover INTEGER,          -- 1=home cover, 0=not, 2=push, NULL=unknown
        settled_at_tw TEXT,

        created_at_tw TEXT,
        updated_at_tw TEXT
    );

    -- ✅ NEW features (for base model learning)
    ALTER TABLE games ADD COLUMN IF NOT EXISTS diff_pts DOUBLE PRECISION;
    ALTER TABLE games ADD COLUMN IF NOT EXISTS diff_impact DOUBLE PRECISION;
    ALTER TABLE games ADD COLUMN IF NOT EXISTS diff_recent_w DOUBLE PRECISION;
    ALTER TABLE games ADD COLUMN IF NOT EXISTS diff_b2b DOUBLE PRECISION;
    ALTER TABLE games ADD COLUMN IF NOT EXISTS pin_ok INTEGER;

    -- ✅ NEW outputs
    ALTER TABLE games ADD COLUMN IF NOT EXISTS p_raw DOUBLE PRECISION;
    ALTER TABLE games ADD COLUMN IF NOT EXISTS p_cal DOUBLE PRECISION;

    -- ✅ player stats cache: store league-wide player features in Supabase
    CREATE TABLE IF NOT EXISTS player_stats_cache (
        season TEXT NOT NULL,
        player_id INTEGER NOT NULL,
        player_name TEXT,
        team_id INTEGER,
        gp DOUBLE PRECISION,
        min DOUBLE PRECISION,
        pts DOUBLE PRECISION,
        reb DOUBLE PRECISION,
        ast DOUBLE PRECISION,
        stl DOUBLE PRECISION,
        blk DOUBLE PRECISION,
        tov DOUBLE PRECISION,
        impact DOUBLE PRECISION,
        norm TEXT,
        updated_at_tw TEXT,
        PRIMARY KEY (season, player_id)
    );
    CREATE INDEX IF NOT EXISTS idx_player_stats_cache_season_team ON player_stats_cache (season, team_id);

    CREATE INDEX IF NOT EXISTS idx_games_date ON games (game_date_us);
    """
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        conn.close()

def load_player_stats_cache(season: str) -> pd.DataFrame:
    conn = pg_conn()
    try:
        sql = """
        SELECT season, player_id, player_name, team_id, gp, min, pts, reb, ast, stl, blk, tov, impact, norm, updated_at_tw
        FROM player_stats_cache
        WHERE season = %s
        ORDER BY impact DESC NULLS LAST, gp DESC NULLS LAST, player_name ASC NULLS LAST
        """
        df = pd.read_sql(sql, conn, params=(season,))
        if not df.empty:
            df.columns = [str(c).upper() for c in df.columns]
        return df
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()

def load_player_stats_cache_updated_at(season: str) -> str:
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(updated_at_tw)
                FROM player_stats_cache
                WHERE season = %s
                """,
                (season,),
            )
            row = cur.fetchone()
        return str(row[0]) if row and row[0] else ""
    except Exception:
        return ""
    finally:
        conn.close()

def save_player_stats_cache(season: str, ps: pd.DataFrame) -> int:
    if ps is None or ps.empty:
        return 0

    now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")
    df = ps.copy()
    if "PLAYER_ID" not in df.columns:
        return 0

    df["season"] = season
    df["player_id"] = pd.to_numeric(df["PLAYER_ID"], errors="coerce").fillna(0).astype(int)
    df["player_name"] = df.get("PLAYER_NAME", pd.Series([None] * len(df)))
    df["team_id"] = pd.to_numeric(df.get("TEAM_ID"), errors="coerce") if "TEAM_ID" in df.columns else None
    df["gp"] = pd.to_numeric(df.get("GP"), errors="coerce")
    df["min"] = pd.to_numeric(df.get("MIN"), errors="coerce")
    df["pts"] = pd.to_numeric(df.get("PTS"), errors="coerce")
    df["reb"] = pd.to_numeric(df.get("REB"), errors="coerce")
    df["ast"] = pd.to_numeric(df.get("AST"), errors="coerce")
    df["stl"] = pd.to_numeric(df.get("STL"), errors="coerce")
    df["blk"] = pd.to_numeric(df.get("BLK"), errors="coerce")
    df["tov"] = pd.to_numeric(df.get("TOV"), errors="coerce")
    df["impact"] = pd.to_numeric(df.get("IMPACT"), errors="coerce")
    df["norm"] = df.get("NORM", pd.Series([None] * len(df)))
    df["updated_at_tw"] = now_tw

    payload_cols = ["season", "player_id", "player_name", "team_id", "gp", "min", "pts", "reb", "ast", "stl", "blk", "tov", "impact", "norm", "updated_at_tw"]
    rows = []
    for _, r in df[payload_cols].iterrows():
        rows.append(tuple(None if pd.isna(v) else v for v in r.tolist()))

    sql = """
    INSERT INTO player_stats_cache (
        season, player_id, player_name, team_id, gp, min, pts, reb, ast, stl, blk, tov, impact, norm, updated_at_tw
    ) VALUES %s
    ON CONFLICT (season, player_id) DO UPDATE SET
        player_name = EXCLUDED.player_name,
        team_id = EXCLUDED.team_id,
        gp = EXCLUDED.gp,
        min = EXCLUDED.min,
        pts = EXCLUDED.pts,
        reb = EXCLUDED.reb,
        ast = EXCLUDED.ast,
        stl = EXCLUDED.stl,
        blk = EXCLUDED.blk,
        tov = EXCLUDED.tov,
        impact = EXCLUDED.impact,
        norm = EXCLUDED.norm,
        updated_at_tw = EXCLUDED.updated_at_tw
    """
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, rows, page_size=500)
        conn.commit()
        return len(rows)
    finally:
        conn.close()

def get_player_stats_cached(season: str = "2025-26", force_refresh: bool = False) -> pd.DataFrame:
    today_tw = datetime.now(tw_tz).strftime("%Y-%m-%d")
    cached = load_player_stats_cache(season)

    if not force_refresh and not cached.empty:
        updated_at = load_player_stats_cache_updated_at(season)
        if updated_at.startswith(today_tw):
            return cached

    ps = get_player_stats(season=season)
    if not ps.empty:
        try:
            save_player_stats_cache(season, ps)
        except Exception:
            pass
    cached = load_player_stats_cache(season)
    return cached if not cached.empty else ps

def upsert_game_row(row: dict):
    now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")
    row = dict(row)
    row.setdefault("created_at_tw", now_tw)
    row["updated_at_tw"] = now_tw

    cols = list(row.keys())
    vals = [row[c] for c in cols]
    placeholders = ",".join(["%s"] * len(cols))
    updates = ",".join([f"{c}=EXCLUDED.{c}" for c in cols if c != "game_id"])

    sql = f"""
    INSERT INTO games ({",".join(cols)})
    VALUES ({placeholders})
    ON CONFLICT (game_id) DO UPDATE SET
      {updates};
    """

    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, vals)
        conn.commit()
        return 1
    finally:
        conn.close()

def bulk_upsert(rows: list[dict]):
    if not rows:
        return 0

    # Dedup by game_id first to prevent
    # ON CONFLICT ... cannot affect row a second time
    dedup = {}
    for r in rows:
        gid = str((r or {}).get("game_id") or "").strip()
        if not gid:
            continue
        dedup[gid] = dict(r)

    if not dedup:
        return 0

    if len(dedup) != len(rows):
        st.info(f"ℹ️ DB bulk_upsert 去重：{len(rows)} -> {len(dedup)}（game_id）")

    now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")
    rows_dedup = list(dedup.values())

    all_cols = sorted(set().union(*[r.keys() for r in rows_dedup]))
    if "game_id" not in all_cols:
        return 0
    if "created_at_tw" not in all_cols:
        all_cols.append("created_at_tw")
    if "updated_at_tw" not in all_cols:
        all_cols.append("updated_at_tw")

    values = []
    for r in rows_dedup:
        rr = dict(r)
        rr.setdefault("created_at_tw", now_tw)
        rr["updated_at_tw"] = now_tw
        values.append([rr.get(c, None) for c in all_cols])

    updates = ",".join([f"{c}=EXCLUDED.{c}" for c in all_cols if c != "game_id"])
    sql = f"""
    INSERT INTO games ({",".join(all_cols)})
    VALUES %s
    ON CONFLICT (game_id) DO UPDATE SET
      {updates};
    """

    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, values, page_size=200)
        conn.commit()
        return len(values)
    finally:
        conn.close()


def init_db_write_stats():
    st.session_state.setdefault("db_write_stats", {
        "auto_ok": 0,
        "auto_fail": 0,
        "manual_ok": 0,
        "manual_fail": 0,
        "last_error": "",
    })


def add_db_write_stats(channel: str, ok: int = 0, fail: int = 0, err: str = ""):
    init_db_write_stats()
    stats = st.session_state["db_write_stats"]
    ok_key = f"{channel}_ok"
    fail_key = f"{channel}_fail"
    stats[ok_key] = int(stats.get(ok_key, 0)) + int(ok)
    stats[fail_key] = int(stats.get(fail_key, 0)) + int(fail)
    if err:
        stats["last_error"] = str(err)


def render_db_write_stats_panel():
    init_db_write_stats()
    s = st.session_state["db_write_stats"]
    with st.expander("🗄️ DB 寫入狀態", expanded=False):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Auto 寫入成功", int(s.get("auto_ok", 0)))
        c2.metric("Auto 寫入失敗", int(s.get("auto_fail", 0)))
        c3.metric("Manual 寫入成功", int(s.get("manual_ok", 0)))
        c4.metric("Manual 寫入失敗", int(s.get("manual_fail", 0)))
        if s.get("last_error"):
            st.caption(f"最近錯誤：{s['last_error']}")

def get_scoreboard_status_map(game_date_us: str) -> dict:
    """
    用 scoreboardv2 拿到比賽狀態與比分
    回傳 keyed by (away_abbr, home_abbr): {status, away_score, home_score}
    """
    sbx = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=game_date_us)
    out = {}
    if sbx.empty:
        return out

    for _, r in sbx.iterrows():
        try:
            hid = int(r.get("HOME_TEAM_ID"))
            aid = int(r.get("VISITOR_TEAM_ID"))
            home_abbr = ID_MAP.get(hid)
            away_abbr = ID_MAP.get(aid)
            if not home_abbr or not away_abbr:
                continue

            hs = r.get("HOME_TEAM_SCORE", None)
            as_ = r.get("VISITOR_TEAM_SCORE", None)
            stxt = str(r.get("GAME_STATUS_TEXT", "")).lower()

            if "final" in stxt:
                status = "final"
            elif ("q" in stxt) or ("half" in stxt) or ("end" in stxt) or ("ot" in stxt):
                status = "in_progress"
            else:
                status = "scheduled"

            out[(away_abbr, home_abbr)] = {
                "status": status,
                "away_score": int(as_) if as_ is not None and str(as_).isdigit() else None,
                "home_score": int(hs) if hs is not None and str(hs).isdigit() else None,
            }
        except Exception:
            continue
    return out

def settle_cover(home_score: int, away_score: int, home_spread: float):
    """
    主隊盤口 home_spread：主讓負、主受讓正
    adjusted = home_score + home_spread
    """
    if home_score is None or away_score is None or home_spread is None:
        return None
    adjusted = float(home_score) + float(home_spread)
    if adjusted > float(away_score):
        return 1
    if adjusted < float(away_score):
        return 0
    return 2

def update_results_and_settle(game_date_us: str):
    """
    讀 DB 裡該日期 games，若 scoreboard 顯示 final，就更新比分與 cover
    """
    status_map = get_scoreboard_status_map(game_date_us)
    if not status_map:
        return 0

    conn = pg_conn()
    updated = 0
    now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT game_id, away_abbr, home_abbr, home_spread FROM games WHERE game_date_us=%s",
                (game_date_us,),
            )
            rows = cur.fetchall()

            for game_id, away_abbr, home_abbr, home_spread in rows:
                key = (away_abbr, home_abbr)
                if key not in status_map:
                    continue
                s = status_map[key]
                status = s["status"]
                away_score = s["away_score"]
                home_score = s["home_score"]

                cover = None
                settled_at = None
                if status == "final" and home_score is not None and away_score is not None:
                    cover = settle_cover(home_score, away_score, home_spread)
                    settled_at = now_tw

                cur.execute(
                    """
                    UPDATE games SET
                      status=%s,
                      away_score=%s,
                      home_score=%s,
                      cover=COALESCE(%s, cover),
                      settled_at_tw=COALESCE(%s, settled_at_tw),
                      updated_at_tw=%s
                    WHERE game_id=%s
                    """,
                    (status, away_score, home_score, cover, settled_at, now_tw, game_id),
                )
                updated += 1

        conn.commit()
        return updated
    finally:
        conn.close()

# =========================================================
# NEW) Load base model + calibrator from DB
# =========================================================
@st.cache_data(ttl=600)
def load_model_from_registry(model_name: str):
    """
    return: (model_or_None, info_dict)
    """
    try:
        ensure_model_registry()
        conn = pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT model_version, payload_base64, trained_rows, metrics, created_at_tw
                    FROM model_registry
                    WHERE model_name=%s
                    LIMIT 1
                """, (model_name,))
                row = cur.fetchone()
        finally:
            conn.close()

        if not row:
            return None, {"ok": False, "reason": "no_model_row"}

        model_version, payload_b64, trained_rows, metrics, created_at_tw = row
        if not payload_b64:
            return None, {"ok": False, "reason": "empty_payload"}

        model = pickle.loads(base64.b64decode(payload_b64.encode("utf-8")))
        info = {
            "ok": True,
            "model_name": model_name,
            "model_version": model_version,
            "trained_rows": trained_rows,
            "created_at_tw": created_at_tw,
            "metrics": metrics if isinstance(metrics, dict) else None,
        }
        return model, info
    except Exception as e:
        return None, {"ok": False, "reason": f"load_error: {e}"}

def load_first_available_model(model_names: list[str]):
    last_info = {"ok": False, "reason": "no_model_candidates"}
    for name in model_names:
        model, info = load_model_from_registry(name)
        if info.get("ok"):
            info = dict(info)
            info["selected_from"] = model_names
            return model, info
        last_info = info
    merged = dict(last_info)
    merged["selected_from"] = model_names
    return None, merged

def clamp01(x: float, lo: float = 0.001, hi: float = 0.999) -> float:
    try:
        x = float(x)
    except Exception:
        return lo
    if x < lo: return lo
    if x > hi: return hi
    return x

def predict_p_raw(base_model, feats: dict, fallback_edge: float, model_info: dict | None = None) -> float:
    """
    base model exists => predict_proba
    else => fallback sigmoid from f_edge
    """
    if base_model is not None:
        # legacy classifier path
        if hasattr(base_model, "predict_proba"):
            try:
                X = pd.DataFrame([feats])
                p = float(base_model.predict_proba(X)[0, 1])
                return clamp01(p)
            except Exception:
                pass

        # regressor / generic predictor fallback (e.g. margin-like output)
        if hasattr(base_model, "predict"):
            try:
                model_feats = ((model_info or {}).get("metrics") or {}).get("features") or []
                if isinstance(model_feats, list) and len(model_feats) > 0:
                    row = {k: float(feats.get(k) or 0.0) for k in model_feats}
                    X = pd.DataFrame([row], columns=model_feats)
                else:
                    X = pd.DataFrame([feats])
                pred = float(base_model.predict(X)[0])
                # If model output already looks like probability, use it directly
                if 0.0 <= pred <= 1.0:
                    return clamp01(pred)
                # Otherwise treat as margin-like signal and map by logistic curve
                return clamp01(calc_cover_prob(pred + float(feats.get("home_spread") or 0.0)))
            except Exception:
                pass

    return clamp01(calc_cover_prob(fallback_edge))

def calibrate_p(iso_model, p_raw: float, edge_input: float | None = None, iso_info: dict | None = None) -> float:
    """
    calibrator exists => iso.predict
    else => return p_raw
    """
    if iso_model is not None:
        try:
            cal_input_mode = ((iso_info or {}).get("metrics") or {}).get("calibration_input")
            x = float(edge_input) if (cal_input_mode == "pred_margin_plus_home_spread" and edge_input is not None) else float(p_raw)
            p = float(iso_model.predict([x])[0])
            return clamp01(p)
        except Exception:
            pass
    return clamp01(p_raw)


def fetch_scoreboard_with_fallback(game_date_us: str) -> pd.DataFrame:
    """Prefer nba_api scoreboardv2; fallback to ESPN scoreboard when nba_api is empty."""
    sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=game_date_us)
    if not sb.empty and "HOME_TEAM_ID" in sb.columns and "VISITOR_TEAM_ID" in sb.columns:
        return sb

    # ESPN fallback to reduce blank-page incidents when nba_api intermittently returns empty.
    try:
        ymd = datetime.strptime(str(game_date_us), "%m/%d/%Y").strftime("%Y%m%d")
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            params={"dates": ymd, "limit": 300},
            timeout=20,
        )
        r.raise_for_status()
        events = (r.json() or {}).get("events") or []

        abbr_to_id = {v: k for k, v in ID_MAP.items()}
        rows = []
        for ev in events:
            comps = ev.get("competitions") or []
            if not comps:
                continue
            comp = comps[0]
            home_id, away_id = None, None
            for c in (comp.get("competitors") or []):
                team = c.get("team") or {}
                abbr = norm_team_abbr(team.get("abbreviation"))
                tid = abbr_to_id.get(abbr)
                if tid is None:
                    continue
                if (c.get("homeAway") or "").lower() == "home":
                    home_id = int(tid)
                else:
                    away_id = int(tid)
            if home_id is not None and away_id is not None:
                rows.append({"HOME_TEAM_ID": home_id, "VISITOR_TEAM_ID": away_id})

        if rows:
            return pd.DataFrame(rows)
    except Exception:
        pass

    return sb

# =========================================================
# 3) 賽程抓取（先決定目標日期，再拉賽程）
# =========================================================
def get_target_scoreboard() -> tuple[str, pd.DataFrame]:
    now_us = datetime.now(us_east_tz)
    target_date_us = now_us.strftime("%m/%d/%Y")
    sb = fetch_scoreboard_with_fallback(target_date_us)

    valid = False
    if not sb.empty and "HOME_TEAM_ID" in sb.columns:
        sb_filtered = sb[sb["HOME_TEAM_ID"].isin(VALID_TEAM_IDS)]
        valid = len(sb_filtered) > 0

    if not valid:
        target_date_us = (now_us + timedelta(days=1)).strftime("%m/%d/%Y")
        sb = fetch_scoreboard_with_fallback(target_date_us)

    return target_date_us, sb


def load_games_for_date_from_db(game_date_us: str, season: str) -> pd.DataFrame:
    conn = pg_conn()
    try:
        sql = """
        SELECT
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
            diff_pts,
            diff_impact,
            diff_recent_w,
            diff_b2b,
            pin_ok,
            p_raw,
            p_cal,
            status,
            updated_at_tw
        FROM games
        WHERE season = %s AND game_date_us = %s
        ORDER BY game_id ASC
        """
        return pd.read_sql(sql, conn, params=(season, game_date_us))
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def get_target_games_from_db(season: str) -> tuple[str, pd.DataFrame]:
    now_us = datetime.now(us_east_tz)
    d0 = now_us.strftime("%m/%d/%Y")
    d1 = (now_us + timedelta(days=1)).strftime("%m/%d/%Y")

    g0 = load_games_for_date_from_db(d0, season)
    if not g0.empty:
        return d0, g0

    g1 = load_games_for_date_from_db(d1, season)
    if not g1.empty:
        return d1, g1

    # 最後退而求其次：回傳最近更新的一天，避免整頁空白
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT game_date_us
                FROM games
                WHERE season=%s
                GROUP BY game_date_us
                ORDER BY MAX(updated_at_tw) DESC NULLS LAST
                LIMIT 1
                """,
                (season,),
            )
            row = cur.fetchone()
        if row and row[0]:
            d2 = str(row[0])
            g2 = load_games_for_date_from_db(d2, season)
            if not g2.empty:
                return d2, g2
    except Exception:
        pass
    finally:
        conn.close()

    return d0, pd.DataFrame()


def build_all_games_data_from_db_rows(df_games: pd.DataFrame) -> list[dict]:
    all_games = []
    if df_games is None or df_games.empty:
        return all_games

    for _, r in df_games.iterrows():
        h_abbr = norm_team_abbr(r.get("home_abbr"))
        a_abbr = norm_team_abbr(r.get("away_abbr"))

        h_cn = TEAM_NAME_CH.get(h_abbr, h_abbr)
        a_cn = TEAM_NAME_CH.get(a_abbr, a_abbr)

        line_source = str(r.get("line_source") or "")
        pin_ok_raw = r.get("pin_ok")
        pin_ok = bool(pin_ok_raw) if pin_ok_raw is not None and not pd.isna(pin_ok_raw) else ("pinnacle" in line_source.lower())

        sp = r.get("home_spread")
        oh = r.get("home_odds")
        oa = r.get("away_odds")
        base_diff = r.get("base_diff")

        all_games.append(
            {
                "game_id": str(r.get("game_id")),
                "label": f"{a_cn}(客) @ {h_cn}(主)",
                "base_diff": float(base_diff) if base_diff is not None and not pd.isna(base_diff) else 0.0,
                "h_pkg": {"pts": 0.0, "impact": 0.0, "df": pd.DataFrame(), "inj": pd.DataFrame(), "b2b": False, "recent_w": 0.5},
                "a_pkg": {"pts": 0.0, "impact": 0.0, "df": pd.DataFrame(), "inj": pd.DataFrame(), "b2b": False, "recent_w": 0.5},
                "h_cn": h_cn,
                "a_cn": a_cn,
                "h_abbr": h_abbr,
                "a_abbr": a_abbr,
                "pin_ok": bool(pin_ok),
                "pin_ok_int": 1 if pin_ok else 0,
                "pin_home_sp": float(sp) if sp is not None and not pd.isna(sp) else 0.0,
                "pin_home_od": float(oh) if oh is not None and not pd.isna(oh) else 1.90,
                "pin_away_od": float(oa) if oa is not None and not pd.isna(oa) else 1.90,
                "diff_pts": float(r.get("diff_pts")) if r.get("diff_pts") is not None and not pd.isna(r.get("diff_pts")) else 0.0,
                "diff_impact": float(r.get("diff_impact")) if r.get("diff_impact") is not None and not pd.isna(r.get("diff_impact")) else 0.0,
                "diff_recent_w": float(r.get("diff_recent_w")) if r.get("diff_recent_w") is not None and not pd.isna(r.get("diff_recent_w")) else 0.0,
                "diff_b2b": float(r.get("diff_b2b")) if r.get("diff_b2b") is not None and not pd.isna(r.get("diff_b2b")) else 0.0,
                "db_line_source": line_source,
            }
        )

    return all_games

# =========================================================
# 4) 球員資料（全聯盟）— cache
# =========================================================
@st.cache_data(ttl=3600)
def get_player_career_per_game(player_id: int) -> dict:
    """Return career per-game box profile for a player. Empty dict means unavailable."""
    df = fetch_safe_df(playercareerstats.PlayerCareerStats, player_id=str(player_id), per_mode36="PerGame")
    if df.empty:
        return {}

    # Prefer NBA regular season career total row if provided.
    if "LEAGUE_ID" in df.columns:
        nba = df[df["LEAGUE_ID"].astype(str) == "00"].copy()
        if not nba.empty:
            df = nba

    if "GP" not in df.columns:
        return {}

    # In some API shapes there is a single career row already; otherwise aggregate weighted per-game by GP.
    keep_cols = [c for c in ["GP", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV"] if c in df.columns]
    if "GP" not in keep_cols:
        return {}

    part = df[keep_cols].copy()
    part["GP"] = pd.to_numeric(part["GP"], errors="coerce").fillna(0)
    total_gp = float(part["GP"].sum())
    if total_gp <= 0:
        return {}

    out = {"GP": total_gp}
    for c in ["MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV"]:
        if c in part.columns:
            v = pd.to_numeric(part[c], errors="coerce").fillna(0)
            out[c] = float((v * part["GP"]).sum() / total_gp)
    return out


@st.cache_data(ttl=3600)
def get_player_stats(season: str = "2025-26") -> pd.DataFrame:
    # ── 優先從 nba_cache（由 cache_nba.py 背景工作寫入）讀取，避免 500+ 次 PlayerCareerStats API ──
    from_cache = False
    cached_payload = nba_cache_get(f"player_stats:{season}")
    if cached_payload and cached_payload.get("rows"):
        ps = pd.DataFrame(cached_payload["rows"])
        from_cache = True
    else:
        ps = fetch_safe_df(
            leaguedashplayerstats.LeagueDashPlayerStats,
            season=season,
            per_mode_detailed="PerGame",
        )

    if ps.empty or "TEAM_ID" not in ps.columns or "PLAYER_NAME" not in ps.columns:
        return pd.DataFrame(columns=["PLAYER_NAME", "TEAM_ID", "PTS", "IMPACT", "NORM", "GP", "MIN"])

    for c in ["PLAYER_ID", "GP", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV"]:
        if c not in ps.columns:
            ps[c] = 0

    ps = ps[(ps["GP"] >= 5) & (ps["MIN"] >= 10)].copy()

    # 只有在即時拉 NBA API 時才額外查詢 PlayerCareerStats（每位球員一次請求）
    # 從 nba_cache 讀取時直接用賽季統計計算 IMPACT，跳過 500+ 次 API 呼叫
    if not from_cache and not ps.empty and "PLAYER_ID" in ps.columns:
        career_rows = []
        for pid in ps["PLAYER_ID"].tolist():
            prof = get_player_career_per_game(int(pid))
            if prof:
                career_rows.append({"PLAYER_ID": int(pid), **prof})

        if career_rows:
            career_df = pd.DataFrame(career_rows)
            ps = ps.merge(career_df, on="PLAYER_ID", how="left", suffixes=("", "_career"))
            for c in ["MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV"]:
                c2 = f"{c}_career"
                if c2 in ps.columns:
                    ps[c] = pd.to_numeric(ps[c2], errors="coerce").fillna(pd.to_numeric(ps[c], errors="coerce").fillna(0))

    ps["IMPACT"] = (
        ps["PTS"]
        + ps["REB"] * 1.1
        + ps["AST"] * 1.5
        + (ps["STL"] + ps["BLK"]) * 2
        - ps["TOV"] * 2
    )
    ps["NORM"] = ps["PLAYER_NAME"].astype(str).map(norm_name)
    return ps

# =========================================================
# 5) 傷病報告（ESPN）— cache（更保守判讀）
# =========================================================
@st.cache_data(ttl=900)
def get_injuries() -> pd.DataFrame:
    inj_list = []
    try:
        url = "https://www.espn.com/nba/injuries"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=12)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        tables = soup.select(".ResponsiveTable") or soup.select("section")

        for table in tables:
            title_el = table.select_one(".Table__Title") or table.find(["h2", "h3"])
            if not title_el:
                continue
            t_name = title_el.get_text(strip=True)
            t_name_norm = t_name.lower()

            t_abbr = None
            for abbr, info in TEAM_MAP.items():
                if info[0].lower() in t_name_norm:
                    t_abbr = abbr
                    break
            if not t_abbr:
                for abbr, info in TEAM_MAP.items():
                    eng_tokens = [w for w in info[0].lower().split() if len(w) >= 3]
                    if any(tok in t_name_norm for tok in eng_tokens):
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
                raw_reason = cols[-1].get_text(" ", strip=True) if len(cols) >= 3 else "無"

                out_kw = ["out", "ruled out", "will not play", "inactive", "suspended"]
                q_kw   = ["questionable", "doubtful", "gtd", "day-to-day", "game time decision"]
                ok_kw  = ["available", "will play", "probable"]

                is_out = any(k in row_text for k in out_kw)
                is_q   = any(k in row_text for k in q_kw)
                is_ok  = any(k in row_text for k in ok_kw)

                if is_out:
                    status_cn = "❌ [確定缺陣]"
                elif is_q:
                    status_cn = "📋 [觀察名單]"
                elif is_ok:
                    status_cn = "✅ [預計出賽]"
                else:
                    status_cn = "📋 [資訊不足/待確認]"

                inj_list.append(
                    {
                        "NORM": norm_name(raw_player),
                        "球員": raw_player,
                        "狀態": status_cn,
                        "原因": raw_reason,
                        "球隊": t_abbr,
                        "IS_OUT": bool(is_out),
                    }
                )
    except Exception:
        pass

    return pd.DataFrame(inj_list)

# =========================================================
# 6) 隊伍 Context（只針對「今日有賽程」隊伍）— cache
# =========================================================
@st.cache_data(ttl=3600)
def get_team_context(team_ids: list[int], game_date_us: str, season: str = "2025-26") -> dict:
    ctx = {}
    game_day = datetime.strptime(game_date_us, "%m/%d/%Y").date()
    prev_day = game_day - timedelta(days=1)

    for tid in team_ids:
        abbr = ID_MAP.get(tid)
        log = pd.DataFrame()

        # 優先從 nba_cache 讀取（由 cache_nba.py 背景工作寫入），避免即時打 NBA API
        if abbr:
            cached = nba_cache_get(f"team_log:{season}:{norm_team_abbr(abbr)}")
            if cached and cached.get("rows"):
                log = pd.DataFrame(cached["rows"])

        # Fallback：直接打 NBA API
        if log.empty:
            log = fetch_safe_df(teamgamelog.TeamGameLog, team_id=tid, season=season)

        is_b2b, recent_w = False, 0.5

        if not log.empty and "GAME_DATE" in log.columns and "WL" in log.columns:
            log = log.head(15).copy()
            log["GAME_DATE"] = pd.to_datetime(log["GAME_DATE"], errors="coerce").dt.date
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
# 7) Odds API（Pinnacle）抓盤口/賠率 — cache
# =========================================================
@st.cache_data(ttl=900)
def get_pinnacle_odds_for_date(game_date_us: str) -> dict:
    api_key = None
    try:
        api_key = st.secrets.get("ODDS_API_KEY", None)
    except Exception:
        api_key = None

    if not api_key:
        return {}

    url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "spreads",
        "bookmakers": "pinnacle",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }

    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return {}
        data = r.json()
    except Exception:
        return {}

    out = {}
    for g in data:
        try:
            home_name = norm_name(g.get("home_team", ""))
            away_name = norm_name(g.get("away_team", ""))

            home_abbr = ODDS_TEAMNAME_TO_ABBR.get(home_name)
            away_abbr = ODDS_TEAMNAME_TO_ABBR.get(away_name)
            if not home_abbr or not away_abbr:
                continue

            books = g.get("bookmakers", [])
            if not books:
                continue

            bk = None
            for b in books:
                if norm_name(b.get("key", "")) == "pinnacle":
                    bk = b
                    break
            if not bk:
                for b in books:
                    if "pinnacle" in norm_name(b.get("title", "")):
                        bk = b
                        break
            if not bk:
                continue

            mkts = bk.get("markets", [])
            spreads = None
            for m in mkts:
                if m.get("key") == "spreads":
                    spreads = m
                    break
            if not spreads:
                continue

            outcomes = spreads.get("outcomes", [])
            if len(outcomes) < 2:
                continue

            home_spread = None
            home_odds = None
            away_odds = None

            for o in outcomes:
                name = norm_name(o.get("name", ""))
                point = o.get("point", None)
                price = o.get("price", None)
                if point is None or price is None:
                    continue
                if name == home_name:
                    home_spread = float(point)
                    home_odds = float(price)
                elif name == away_name:
                    away_odds = float(price)

            if home_spread is None or home_odds is None or away_odds is None:
                continue

            out[(away_abbr, home_abbr)] = {
                "home_spread": float(home_spread),
                "home_odds": float(home_odds),
                "away_odds": float(away_odds),
                "ok": True,
            }
        except Exception:
            continue

    return out

# =========================================================
# 8) UI 初始化（保留原本配置 + 強制更新按鈕）
# =========================================================
st.set_page_config(page_title="NBA Edge v16.0 + Supabase + ML", layout="wide")

# 建表（只要 secrets 正確，這裡會自動建立/升級 games table）
try:
    db_init()
    ensure_model_registry()
except Exception as e:
    st.error(f"❌ Supabase DB 初始化失敗：{e}")
    st.stop()

# NEW: load models (compatible with both legacy cover-prob and new margin pipeline names)
base_model, base_info = load_first_available_model(["cover_prob_base_model", "margin_base_model"])
iso_model, iso_info   = load_first_available_model(["cover_prob_calibrator", "margin_calibrator"])

h1, h2 = st.columns([0.8, 0.2])
with h1:
    now_tw_str = datetime.now(tw_tz).strftime("%m/%d %H:%M")
    st.title("🏀 NBA Edge 數據預測系統")
    st.caption(f"台灣現在時間：{now_tw_str}")

SEASON_OPTIONS = ["2025-26", "2024-25", "2023-24"]
APP_SEASON = st.sidebar.selectbox("賽季", SEASON_OPTIONS, index=0)
db_only_default = (os.environ.get("APP_DB_ONLY", "1").strip() == "1")
USE_DB_ONLY = st.sidebar.toggle(
    "DB-only 模式（只讀資料庫）",
    value=db_only_default,
    help="開啟後頁面不再即時抓 ESPN/NBA/Odds，改為讀取背景 job 寫入的 games 與 cache。",
)
st.sidebar.caption("球員特徵固定使用生涯場均（抓不到時自動回退當季場均）。")
with h2:
    if st.button("🔄 強制更新（傷病/盤口/數據/模型）"):
        st.cache_data.clear()
        if not USE_DB_ONLY:
            st.session_state["force_player_stats_refresh"] = True
        st.rerun()
    with st.popover("🧠 模型狀態 / 判讀指南"):
        if base_info.get("ok"):
            st.success(
                f"✅ Base model 已載入：{base_info.get('model_name')} | "
                f"{base_info.get('model_version')} | rows={base_info.get('trained_rows')}"
            )
        else:
            st.warning(f"⚠️ Base model 未載入（改用 fallback sigmoid）。原因：{base_info.get('reason')}")

        if iso_info.get("ok"):
            st.success(
                f"✅ Calibrator 已載入：{iso_info.get('model_name')} | "
                f"{iso_info.get('model_version')} | rows={iso_info.get('trained_rows')}"
            )
        else:
            st.warning(f"⚠️ Calibrator 未載入（p_cal=p_raw）。原因：{iso_info.get('reason')}")

        st.markdown(
            "**點數優勢**：模型預測分差與盤口的差距（點數）。\n\n"
            "**盤口優勢**：過盤機率(校正後) - 損益兩平機率（%）。\n\n"
            "**期望報酬**：以過盤機率估算的長期期望（%）。\n\n"
            "**Top picks**：\n"
            "- 候選池：只用 Pinnacle 真盤\n"
            "- 排序：用你輸入的盤口/賠率重新計算（p_cal、EV、edge_value）\n\n"
            "**DB**：每日自動寫入今日賽程；你手動改運彩會寫回；結算後 cover 進入訓練資料。\n"
            "- 建議正式環境使用 DB-only 模式（網站只讀 DB，更新交給背景 job）。\n\n"
            "**模型學習**：Base model 用 DB 已結算資料訓練；Calibrator 校正 p_raw。\n"
        )

# =========================================================
# 9) 主計算：DB-only（只讀資料庫）或 Live API（舊模式）
# =========================================================
if USE_DB_ONLY:
    with st.spinner("⚡ 讀取資料庫快取中..."):
        target_date_us, db_games = get_target_games_from_db(APP_SEASON)

    if db_games.empty:
        st.info("📅 目前資料庫還沒有可用賽程，請先跑 preload/sync job。")
        st.stop()

    now_us_str = datetime.now(us_east_tz).strftime("%m/%d/%Y")
    if target_date_us != now_us_str:
        st.info(f"📅 DB-only：目前顯示 {target_date_us}（資料庫最近可用日期）。")
    else:
        st.success(f"📅 DB-only：正在分析美東今日賽程：{target_date_us}")

    all_games_data = build_all_games_data_from_db_rows(db_games)
else:
    with st.spinner("⚡ 正在同步美東數據中心..."):
        target_date_us, sb = get_target_scoreboard()
        ps_db = get_player_stats_cached(season=APP_SEASON, force_refresh=bool(st.session_state.pop("force_player_stats_refresh", False)))
        inj_db = get_injuries()

    if sb.empty or "HOME_TEAM_ID" not in sb.columns:
        st.info("📅 目前抓不到賽程資料（NBA/ESPN scoreboard 皆回傳空）。請稍後重試。")
        st.stop()

    sb_filtered = sb[sb["HOME_TEAM_ID"].isin(VALID_TEAM_IDS)].copy()

    if sb_filtered.empty:
        st.info(f"📅 {target_date_us}（美東）無有效 NBA 賽程。")
        st.stop()
    else:
        now_us_str = datetime.now(us_east_tz).strftime("%m/%d/%Y")
        if target_date_us != now_us_str:
            st.info(f"📅 今日美東無賽程，已為您自動跳轉至明日：{target_date_us}")
        else:
            st.success(f"📅 正在分析美東今日賽程：{target_date_us}")

    today_team_ids = sorted(set(sb_filtered["HOME_TEAM_ID"].tolist() + sb_filtered["VISITOR_TEAM_ID"].tolist()))
    ctx_db = get_team_context(today_team_ids, game_date_us=target_date_us, season=APP_SEASON)

    if inj_db.empty:
        st.warning("⚠️ 傷病名單目前抓不到（ESPN 可能改版或暫時阻擋），推薦將不會排除傷兵。")

    pinnacle_map = get_pinnacle_odds_for_date(target_date_us)

    all_games_data = []
    existing_game_id_map = load_existing_game_id_map(target_date_us, APP_SEASON)

    for _, row in sb_filtered.iterrows():
        h_id, a_id = row["HOME_TEAM_ID"], row["VISITOR_TEAM_ID"]
        h_abbr, a_abbr = ID_MAP.get(h_id, str(h_id)), ID_MAP.get(a_id, str(a_id))

        def build_pkg(tid: int, abbr: str):
            ctx = ctx_db.get(tid, {"b2b": False, "recent_w": 0.5})

            t_inj = inj_db[inj_db["球隊"] == abbr] if not inj_db.empty else pd.DataFrame()
            out_list = t_inj[t_inj["IS_OUT"]]["NORM"].tolist() if not t_inj.empty else []

            if not ps_db.empty and "TEAM_ID" in ps_db.columns and "NORM" in ps_db.columns:
                active = (
                    ps_db[(ps_db["TEAM_ID"] == tid) & (~ps_db["NORM"].isin(out_list))]
                    .sort_values("IMPACT", ascending=False)
                    .copy()
                )
            else:
                active = pd.DataFrame()

            return {
                "pts": float(active["PTS"].sum()) if not active.empty and "PTS" in active.columns else 0.0,
                "impact": float(active["IMPACT"].mean()) if not active.empty and "IMPACT" in active.columns else 0.0,
                "df": active,
                "inj": t_inj,
                "b2b": bool(ctx["b2b"]),
                "recent_w": float(ctx["recent_w"]),
            }

        h_p, a_p = build_pkg(h_id, h_abbr), build_pkg(a_id, a_abbr)

        diff_pts = float(h_p["pts"] - a_p["pts"])
        diff_impact = float(h_p["impact"] - a_p["impact"])
        diff_recent_w = float(h_p["recent_w"] - a_p["recent_w"])
        diff_b2b = float((1.0 if h_p["b2b"] else 0.0) - (1.0 if a_p["b2b"] else 0.0))

        b2b_v = (-2.5 if h_p["b2b"] else 0) - (-2.5 if a_p["b2b"] else 0)
        recent_v = (h_p["recent_w"] - a_p["recent_w"]) * 5

        base_diff = (h_p["pts"] - a_p["pts"]) * 0.09 + (h_p["impact"] - a_p["impact"]) * 3.8 + 2.5 + b2b_v + recent_v

        game_id = existing_game_id_map.get((norm_team_abbr(a_abbr), norm_team_abbr(h_abbr))) or f"{a_abbr}_{h_abbr}_{target_date_us.replace('/','')}"
        a_cn = TEAM_NAME_CH.get(a_abbr, a_abbr)
        h_cn = TEAM_NAME_CH.get(h_abbr, h_abbr)

        pin = pinnacle_map.get((a_abbr, h_abbr), None)
        pin_ok = bool(pin and pin.get("ok"))
        pin_home_sp = float(pin["home_spread"]) if pin_ok else 0.0
        pin_home_od = float(pin["home_odds"]) if pin_ok else 1.90
        pin_away_od = float(pin["away_odds"]) if pin_ok else 1.90

        all_games_data.append(
            {
                "game_id": game_id,
                "label": f"{a_cn}(客) @ {h_cn}(主)",
                "base_diff": float(base_diff),
                "h_pkg": h_p,
                "a_pkg": a_p,
                "h_cn": h_cn,
                "a_cn": a_cn,
                "h_abbr": h_abbr,
                "a_abbr": a_abbr,
                "pin_ok": pin_ok,
                "pin_ok_int": 1 if pin_ok else 0,
                "pin_home_sp": pin_home_sp,
                "pin_home_od": pin_home_od,
                "pin_away_od": pin_away_od,
                "diff_pts": diff_pts,
                "diff_impact": diff_impact,
                "diff_recent_w": diff_recent_w,
                "diff_b2b": diff_b2b,
            }
        )

# =========================================================
# 10) 指標計算（✅ 使用 Base model + Calibrator）
# =========================================================
EDGE_THRESHOLD = 0.05
EDGE_THRESHOLD_LOW = 0.02  # secondary fallback threshold when < MAX_PICKS qualify at 5%
MAX_PICKS = 4
MAX_GAMES_FOR_PICK = 10

BASE_FEATURES = [
    "home_spread",
    "diff_pts",
    "diff_impact",
    "diff_recent_w",
    "diff_b2b",
    "pin_ok",
    "base_diff",
    "f_edge",
]

def safe_float(x, default):
    try:
        return float(x)
    except Exception:
        return float(default)

def get_market_inputs_for_game(g):
    gid = g["game_id"]
    sp_default = g["pin_home_sp"]
    oh_default = g["pin_home_od"]
    oa_default = g["pin_away_od"]

    def _pick_state(prefix: str, default_val: float) -> float:
        direct_key = f"{prefix}_{gid}"
        if direct_key in st.session_state:
            return safe_float(st.session_state.get(direct_key), default_val)

        cand = [k for k in st.session_state.keys() if str(k).startswith(f"{prefix}_{gid}__")]
        if cand:
            cand.sort()
            return safe_float(st.session_state.get(cand[-1]), default_val)
        return safe_float(default_val, default_val)

    sp = _pick_state("sp", sp_default)
    oh = _pick_state("oh", oh_default)
    oa = _pick_state("oa", oa_default)

    manual = (abs(sp - sp_default) > 1e-9) or (abs(oh - oh_default) > 1e-9) or (abs(oa - oa_default) > 1e-9)

    if manual:
        src = "手動（運彩）✍️"
    elif g["pin_ok"]:
        src = "Pinnacle ✅"
    else:
        src = "Fallback ⚠️"

    return float(sp), float(oh), float(oa), src, manual

def compute_metrics(g, home_spread_input, home_odds, away_odds, base_model, iso_model):
    """
    ✅ 核心：p_raw 由 base_model 產生，p_cal 由 calibrator 校正
    """
    f_edge = float(g["base_diff"] + home_spread_input)

    feats = {
        "home_spread": float(home_spread_input),
        "diff_pts": float(g["diff_pts"]),
        "diff_impact": float(g["diff_impact"]),
        "diff_recent_w": float(g["diff_recent_w"]),
        "diff_b2b": float(g["diff_b2b"]),
        "pin_ok": float(g["pin_ok_int"]),
        "base_diff": float(g["base_diff"]),
        "f_edge": float(f_edge),
    }

    p_raw = predict_p_raw(base_model, feats, fallback_edge=f_edge, model_info=base_info)
    p_cal = calibrate_p(iso_model, p_raw, edge_input=f_edge, iso_info=iso_info)

    pick_team = g["h_cn"] if f_edge > 0 else g["a_cn"]
    odds = home_odds if f_edge > 0 else away_odds

    implied_prob = 1.0 / odds if odds and odds > 0 else 1.0
    edge_value = p_cal - implied_prob
    ev = (p_cal * odds) - 1

    return {
        "f_edge": float(f_edge),
        "edge_points": float(abs(f_edge)),
        "p_raw": float(p_raw),
        "p_cal": float(p_cal),
        "cover_prob": float(p_cal),  # UI 用校正後
        "implied_prob": float(implied_prob),
        "edge_value": float(edge_value),
        "ev": float(ev),
        "pick_team": pick_team,
        "odds_used": float(odds),
    }

# =========================================================
# 11) 自動寫入 DB：把今日賽程先建檔（預設 Pinnacle / fallback）
# =========================================================
init_db_write_stats()
if USE_DB_ONLY:
    st.caption("🗄️ DB-only 模式：頁面載入時不做自動建檔寫回。")
else:
    try:
        auto_rows = []
        for g in all_games_data:
            sp = float(g["pin_home_sp"])
            oh = float(g["pin_home_od"])
            oa = float(g["pin_away_od"])
            src = "Pinnacle ✅" if g["pin_ok"] else "Fallback ⚠️"
            m = compute_metrics(g, sp, oh, oa, base_model, iso_model)

            auto_rows.append({
                "game_id": g["game_id"],
                "game_date_us": target_date_us,
                "season": APP_SEASON,
                "away_abbr": g["a_abbr"],
                "home_abbr": g["h_abbr"],
                "away_name": g["a_cn"],
                "home_name": g["h_cn"],

                "home_spread": sp,
                "home_odds": oh,
                "away_odds": oa,
                "line_source": src,

                "base_diff": float(g["base_diff"]),
                "f_edge": float(m["f_edge"]),
                "cover_prob": float(m["cover_prob"]),
                "implied_prob": float(m["implied_prob"]),
                "edge_value": float(m["edge_value"]),
                "ev": float(m["ev"]),
                "pick_team": str(m["pick_team"]),

                # ✅ ML features + outputs
                "diff_pts": float(g["diff_pts"]),
                "diff_impact": float(g["diff_impact"]),
                "diff_recent_w": float(g["diff_recent_w"]),
                "diff_b2b": float(g["diff_b2b"]),
                "pin_ok": int(g["pin_ok_int"]),
                "p_raw": float(m["p_raw"]),
                "p_cal": float(m["p_cal"]),

                "status": "scheduled",
                "away_score": None,
                "home_score": None,
                "cover": None,
                "settled_at_tw": None,
            })
        n_auto = bulk_upsert(auto_rows)
        add_db_write_stats("auto", ok=int(n_auto or 0), fail=0)
    except Exception as e:
        add_db_write_stats("auto", ok=0, fail=max(1, len(all_games_data)), err=str(e))
        st.warning(f"⚠️ DB 自動建檔失敗（不影響前端運作）：{e}")

with st.expander("ℹ️ 系統主流程 / 數據分析邏輯（點我看）", expanded=False):
    st.markdown(
        "1. **賽程與盤口**：抓取今日賽程 + Pinnacle 盤口（若無則 fallback）。\n"
        "2. **球員與傷病**：整合球員 per-game 與傷病，先做可出賽名單過濾。\n"
        "3. **特徵計算**：為每場建構隊伍特徵（分數、impact、近況、b2b 等）並推導 `base_diff`。\n"
        "4. **機率推論**：先算 `p_raw`，再用 calibrator 得到 `p_cal`。\n"
        "5. **價值計算**：由 `p_cal` 與賠率算 `implied_prob / edge_value / EV`，輸出推薦。\n"
        "6. **資料回寫**：頁面載入與你手動改盤時，會自動 upsert 回 DB。"
    )
    st.caption("補充：下方『更新賽果/結算』只在你要立即手動結算 final 比賽時使用；平常可不按。")

render_db_write_stats_panel()

# =========================================================
# 12) 🔥 今日最能買（候選池=真盤；排序=你輸入）
# =========================================================
st.header("🔥 今日過盤推薦 (Top 4)")

pool_games = all_games_data[:MAX_GAMES_FOR_PICK]

pick_pool = []
for g in pool_games:
    if not g["pin_ok"]:
        continue
    u_sp, u_oh, u_oa, src, manual = get_market_inputs_for_game(g)
    m = compute_metrics(g, u_sp, u_oh, u_oa, base_model, iso_model)
    pick_pool.append({
        "g": g,
        "src": src,
        "manual": manual,
        "home_spread_input": u_sp,
        "home_odds": u_oh,
        "away_odds": u_oa,
        "no_odds_mode": False,
        **m
    })

# No-odds fallback pool: games without Pinnacle data, ranked by model confidence
no_odds_pool = []
for g in pool_games:
    if g["pin_ok"]:
        continue
    m_fallback = compute_metrics(g, 0.0, g["pin_home_od"], g["pin_away_od"], base_model, iso_model)
    no_odds_pool.append({
        "g": g,
        "src": "純勝率 ⚠️",
        "manual": False,
        "home_spread_input": 0.0,
        "home_odds": g["pin_home_od"],
        "away_odds": g["pin_away_od"],
        "no_odds_mode": True,
        **m_fallback
    })

# Primary: edge > 5%
primary = [x for x in pick_pool if x["edge_value"] > EDGE_THRESHOLD]
primary.sort(key=lambda x: (x["cover_prob"], x["edge_value"], x["ev"]), reverse=True)
picks = primary[:MAX_PICKS]

# Secondary: relax to 2% to fill up to MAX_PICKS
if len(picks) < MAX_PICKS:
    existing_gids = {p["g"]["game_id"] for p in picks}
    secondary = [
        x for x in pick_pool
        if EDGE_THRESHOLD_LOW < x["edge_value"] <= EDGE_THRESHOLD
        and x["g"]["game_id"] not in existing_gids
    ]
    secondary.sort(key=lambda x: (x["cover_prob"], x["edge_value"], x["ev"]), reverse=True)
    picks += secondary[:MAX_PICKS - len(picks)]

# Pure win-rate fallback: fill remaining with no-odds games sorted by model confidence
if len(picks) < MAX_PICKS:
    existing_gids = {p["g"]["game_id"] for p in picks}
    no_odds_sorted = sorted(
        [x for x in no_odds_pool if x["g"]["game_id"] not in existing_gids],
        key=lambda x: abs(x["p_cal"] - 0.5),
        reverse=True,
    )
    picks += no_odds_sorted[:MAX_PICKS - len(picks)]

if len(picks) == 0:
    st.info("依挑場規則：前 10 場裡，沒有任何一場符合門檻（盤口優勢 > 5% 或模型信心足夠）。建議不買、不硬湊。")
else:
    if len(picks) == 1:
        st.success("🎯 今日只有 1 場符合門檻：建議只買單場（或分注單場），不要硬湊串關。")
    else:
        n_no_odds = sum(1 for p in picks if p.get("no_odds_mode"))
        n_secondary = sum(1 for p in picks if not p.get("no_odds_mode") and EDGE_THRESHOLD_LOW < p["edge_value"] <= EDGE_THRESHOLD)
        note = f"已依規則挑出 {len(picks)} 場（最多四場）"
        if n_secondary:
            note += f"，其中 {n_secondary} 場為放寬門檻（盤口優勢 2-5%）"
        if n_no_odds:
            note += f"，其中 {n_no_odds} 場為純勝率補位（無真實盤口）"
        st.success(f"🎯 今日最能買：{note}。")

    cols = st.columns(len(picks))
    for idx, item in enumerate(picks):
        g = item["g"]
        with cols[idx]:
            with st.container(border=True):
                st.subheader(f"精選 {idx+1}")
                st.write(f"**{g['label']}**")
                st.success(f"首選：{item['pick_team']}")
                if item.get("no_odds_mode"):
                    st.warning("⚠️ 純勝率模式（無盤口）")
                    st.caption(f"盤口來源：{item['src']}（依模型勝率排序）")
                else:
                    label = "放寬門檻" if EDGE_THRESHOLD_LOW < item["edge_value"] <= EDGE_THRESHOLD else "主力門檻"
                    st.caption(f"盤口來源：{item['src']}（{label}；候選池=真盤；排序=你輸入的盤口/賠率）")

                st.write(
                    f"過盤機率(校正)：**{item['cover_prob']*100:.1f}%** | "
                    f"損益兩平：**{item['implied_prob']*100:.1f}%**"
                )
                st.metric("盤口優勢", f"{item['edge_value']*100:+.1f}%")
                st.write(
                    f"主隊盤口：**{item['home_spread_input']:+.1f}** | "
                    f"主賠：**{item['home_odds']:.2f}** | 客賠：**{item['away_odds']:.2f}**"
                )
                st.write(f"點數優勢：**{item['edge_points']:.1f}** | 期望報酬：**{item['ev']*100:+.1f}%**")
                st.caption(f"p_raw={item['p_raw']:.3f} → p_cal={item['p_cal']:.3f}")

# =========================================================
# 13) 手動結算（可選）：平常可不按
# =========================================================
st.divider()
st.caption("✅ 本頁面會自動寫入 DB。下方結算按鈕僅用於你想『立即』把 final 比賽寫入 cover。")
with st.expander("🧾 手動更新賽果 / 結算（可選）", expanded=False):
    cA, cB = st.columns([0.6, 0.4])
    with cA:
        if st.button("立即結算 Final 比賽（寫入 cover）"):
            try:
                n = update_results_and_settle(target_date_us)
                st.success(f"已掃描並更新 {n} 筆（非 Final 的 cover 會維持空值）")
                # 若你剛好同日重訓模型，這裡也順便清 cache
                st.cache_data.clear()
            except Exception as e:
                st.error(f"結算失敗：{e}")
    with cB:
        st.caption("cover 定義：home_score + home_spread vs away_score；相等為 push(2)")

st.divider()

# =========================================================
# 14) 🎯 全部場次與實時計算（保留原 UI；預設帶 Pinnacle）
# =========================================================
st.header("🎯 全部場次與實時計算")

for i in range(0, len(all_games_data), 3):
    cols = st.columns(3)
    for j, g in enumerate(all_games_data[i : i + 3]):
        with cols[j]:
            with st.container(border=True):
                st.subheader(g["label"])
                gid = g["game_id"]
                gid_key = f"{gid}__{i}_{j}"

                sp_default = g["pin_home_sp"]
                oh_default = g["pin_home_od"]
                oa_default = g["pin_away_od"]

                u_sp = st.number_input(
                    "主隊盤口（主讓分填負｜主受讓填正）",
                    min_value=-60.0,
                    max_value=60.0,
                    value=safe_float(st.session_state.get(f"sp_{gid_key}", sp_default), sp_default),
                    step=0.5,
                    key=f"sp_{gid_key}",
                )
                u_oh = st.number_input(
                    "主賠（可手動改運彩）",
                    min_value=1.01,
                    max_value=10.0,
                    value=safe_float(st.session_state.get(f"oh_{gid_key}", oh_default), oh_default),
                    step=0.01,
                    key=f"oh_{gid_key}",
                )
                u_oa = st.number_input(
                    "客賠（可手動改運彩）",
                    min_value=1.01,
                    max_value=10.0,
                    value=safe_float(st.session_state.get(f"oa_{gid_key}", oa_default), oa_default),
                    step=0.01,
                    key=f"oa_{gid_key}",
                )

                manual = (abs(float(u_sp) - sp_default) > 1e-9) or (abs(float(u_oh) - oh_default) > 1e-9) or (abs(float(u_oa) - oa_default) > 1e-9)
                if manual:
                    src = "手動（運彩）✍️"
                elif g["pin_ok"]:
                    src = "Pinnacle ✅"
                else:
                    src = "Fallback ⚠️"

                m = compute_metrics(g, float(u_sp), float(u_oh), float(u_oa), base_model, iso_model)

                st.caption(f"盤口來源：{src}（Top picks 候選池只用 Pinnacle ✅）")
                st.write(f"過盤機率(校正)：**{m['cover_prob']*100:.1f}%** | 點數優勢：**{m['edge_points']:.1f}**")
                st.write(f"盤口優勢：**{m['edge_value']*100:+.1f}%** | 期望報酬：**{m['ev']*100:+.1f}%**")
                st.caption(f"p_raw={m['p_raw']:.3f} → p_cal={m['p_cal']:.3f}")

                if g["pin_ok"] and m["edge_value"] > EDGE_THRESHOLD:
                    st.success(f"🔥 符合挑場門檻（真盤候選 + 盤口優勢 > 5%）：{m['pick_team']}")
                else:
                    st.info(f"建議：{m['pick_team']}")

                # ✅ 寫回 DB：你手動輸入的盤口/賠率 + 最新機率 + features
                if not USE_DB_ONLY:
                    try:
                        upsert_game_row({
                            "game_id": gid,
                            "game_date_us": target_date_us,
                            "season": APP_SEASON,
                            "away_abbr": g["a_abbr"],
                            "home_abbr": g["h_abbr"],
                            "away_name": g["a_cn"],
                            "home_name": g["h_cn"],

                            "home_spread": float(u_sp),
                            "home_odds": float(u_oh),
                            "away_odds": float(u_oa),
                            "line_source": src,

                            "base_diff": float(g["base_diff"]),
                            "f_edge": float(m["f_edge"]),
                            "cover_prob": float(m["cover_prob"]),
                            "implied_prob": float(m["implied_prob"]),
                            "edge_value": float(m["edge_value"]),
                            "ev": float(m["ev"]),
                            "pick_team": str(m["pick_team"]),

                            "diff_pts": float(g["diff_pts"]),
                            "diff_impact": float(g["diff_impact"]),
                            "diff_recent_w": float(g["diff_recent_w"]),
                            "diff_b2b": float(g["diff_b2b"]),
                            "pin_ok": int(g["pin_ok_int"]),
                            "p_raw": float(m["p_raw"]),
                            "p_cal": float(m["p_cal"]),
                        })
                        add_db_write_stats("manual", ok=1, fail=0)
                    except Exception as e:
                        add_db_write_stats("manual", ok=0, fail=1, err=str(e))
                        # 不要讓 DB 問題把 UI 卡死
                        pass

# =========================================================
# 15) 🔍 深度查詢（保留原 UI）
# =========================================================
st.divider()
st.header("🔍 深度數據查詢")

sel = st.selectbox("請選擇場次", [g["label"] for g in all_games_data])
if sel:
    curr = next(g for g in all_games_data if g["label"] == sel)

    st.write(
        f"📊 **戰前速報**："
        f"{'🚨 客隊背靠背' if curr['a_pkg']['b2b'] else '✅ 客隊體能正常'} | "
        f"{'🚨 主隊背靠背' if curr['h_pkg']['b2b'] else '✅ 主隊體能正常'}"
    )

    c1, c2 = st.columns(2)
    for col, pkg, side in zip([c1, c2], [curr["h_pkg"], curr["a_pkg"]], ["(主)", "(客)"]):
        with col:
            team_name = curr["h_cn"] if side == "(主)" else curr["a_cn"]
            st.subheader(f"{team_name} {side}")
            st.write(f"近五場勝率: **{pkg['recent_w']*100:.0f}%**")

            if pkg["df"] is not None and not pkg["df"].empty:
                show_cols = [c for c in ["PLAYER_NAME", "PTS", "IMPACT"] if c in pkg["df"].columns]
                st.dataframe(pkg["df"][show_cols].head(12), hide_index=True)
            else:
                st.write("（球員資料不足或 API 暫時不可用）")

            if pkg["inj"] is not None and not pkg["inj"].empty:
                st.dataframe(pkg["inj"][["球員", "狀態", "原因"]], hide_index=True)
            else:
                st.write("✅ 無傷病報告")

st.caption(f"（fallback 機率曲線參數：prob_scale={PROB_SCALE:.1f}；硬性截斷：{int(PROB_FLOOR*100)}%~{int(PROB_CEIL*100)}%）")
