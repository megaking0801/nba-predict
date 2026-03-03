import streamlit as st
from nba_api.stats.endpoints import scoreboardv2, leaguedashplayerstats, teamgamelog
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests, re, unicodedata, time, math
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

ALL_TEAMS = teams.get_teams()
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
    host = st.secrets["SUPABASE_HOST"]
    db   = st.secrets.get("SUPABASE_DB", "postgres")
    user = st.secrets["SUPABASE_USER"]
    pw   = st.secrets["SUPABASE_PASSWORD"]
    port = int(st.secrets.get("SUPABASE_PORT", 5432))

    return psycopg2.connect(
        host=host,
        dbname=db,
        user=user,
        password=pw,
        port=port,
        connect_timeout=8,
        sslmode="require",
    )

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

    CREATE INDEX IF NOT EXISTS idx_games_date ON games (game_date_us);
    """
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        conn.close()

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
    finally:
        conn.close()

def bulk_upsert(rows: list[dict]):
    if not rows:
        return

    # Dedup by game_id first to prevent
    # ON CONFLICT ... cannot affect row a second time
    dedup = {}
    for r in rows:
        gid = str((r or {}).get("game_id") or "").strip()
        if not gid:
            continue
        dedup[gid] = dict(r)

    if not dedup:
        return

    if len(dedup) != len(rows):
        st.info(f"ℹ️ DB bulk_upsert 去重：{len(rows)} -> {len(dedup)}（game_id）")

    now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")
    rows_dedup = list(dedup.values())

    all_cols = sorted(set().union(*[r.keys() for r in rows_dedup]))
    if "game_id" not in all_cols:
        return
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
    finally:
        conn.close()

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

def predict_p_raw(base_model, feats: dict, fallback_edge: float) -> float:
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

def calibrate_p(iso_model, p_raw: float) -> float:
    """
    calibrator exists => iso.predict
    else => return p_raw
    """
    if iso_model is not None:
        try:
            p = float(iso_model.predict([float(p_raw)])[0])
            return clamp01(p)
        except Exception:
            pass
    return clamp01(p_raw)

# =========================================================
# 3) 賽程抓取（先決定目標日期，再拉賽程）
# =========================================================
def get_target_scoreboard() -> tuple[str, pd.DataFrame]:
    now_us = datetime.now(us_east_tz)
    target_date_us = now_us.strftime("%m/%d/%Y")
    sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=target_date_us)

    valid = False
    if not sb.empty and "HOME_TEAM_ID" in sb.columns:
        sb_filtered = sb[sb["HOME_TEAM_ID"].isin(VALID_TEAM_IDS)]
        valid = len(sb_filtered) > 0

    if not valid:
        target_date_us = (now_us + timedelta(days=1)).strftime("%m/%d/%Y")
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=target_date_us)

    return target_date_us, sb

# =========================================================
# 4) 球員資料（全聯盟）— cache
# =========================================================
@st.cache_data(ttl=3600)
def get_player_stats(season: str = "2025-26") -> pd.DataFrame:
    ps = fetch_safe_df(
        leaguedashplayerstats.LeagueDashPlayerStats,
        season=season,
        per_mode_detailed="PerGame",
    )
    if ps.empty or "TEAM_ID" not in ps.columns or "PLAYER_NAME" not in ps.columns:
        return pd.DataFrame(columns=["PLAYER_NAME", "TEAM_ID", "PTS", "IMPACT", "NORM", "GP", "MIN"])

    for c in ["GP", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV"]:
        if c not in ps.columns:
            ps[c] = 0

    ps = ps[(ps["GP"] >= 5) & (ps["MIN"] >= 10)].copy()

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
with h2:
    if st.button("🔄 強制更新（傷病/盤口/數據/模型）"):
        st.cache_data.clear()
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
            "**DB**：每日自動寫入今日賽程；你手動改運彩會寫回；結算後 cover 進入訓練資料。\n\n"
            "**模型學習**：Base model 用 DB 已結算資料訓練；Calibrator 校正 p_raw。\n"
        )

with st.spinner("⚡ 正在同步美東數據中心..."):
    target_date_us, sb = get_target_scoreboard()
    ps_db = get_player_stats(season="2025-26")
    inj_db = get_injuries()

if sb.empty or "HOME_TEAM_ID" not in sb.columns:
    st.info("📅 目前抓不到賽程資料（Scoreboard API 回傳空）。請稍後重試。")
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
ctx_db = get_team_context(today_team_ids, game_date_us=target_date_us, season="2025-26")

if inj_db.empty:
    st.warning("⚠️ 傷病名單目前抓不到（ESPN 可能改版或暫時阻擋），推薦將不會排除傷兵。")

pinnacle_map = get_pinnacle_odds_for_date(target_date_us)

# =========================================================
# 9) 主計算：建立每場 pkg + base_diff（保留你的核心公式）
# =========================================================
all_games_data = []

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

    # diffs (✅ will be stored for ML training)
    diff_pts = float(h_p["pts"] - a_p["pts"])
    diff_impact = float(h_p["impact"] - a_p["impact"])
    diff_recent_w = float(h_p["recent_w"] - a_p["recent_w"])
    diff_b2b = float((1.0 if h_p["b2b"] else 0.0) - (1.0 if a_p["b2b"] else 0.0))

    b2b_v = (-2.5 if h_p["b2b"] else 0) - (-2.5 if a_p["b2b"] else 0)
    recent_v = (h_p["recent_w"] - a_p["recent_w"]) * 5

    base_diff = (h_p["pts"] - a_p["pts"]) * 0.09 + (h_p["impact"] - a_p["impact"]) * 3.8 + 2.5 + b2b_v + recent_v

    game_id = f"{a_abbr}_{h_abbr}_{target_date_us.replace('/','')}"
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
MAX_PICKS = 3
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

    p_raw = predict_p_raw(base_model, feats, fallback_edge=f_edge)
    p_cal = calibrate_p(iso_model, p_raw)

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
            "season": "2025-26",
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
    bulk_upsert(auto_rows)
except Exception as e:
    st.warning(f"⚠️ DB 自動建檔失敗（不影響前端運作）：{e}")

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
        **m
    })

qualified = [x for x in pick_pool if x["edge_value"] > EDGE_THRESHOLD]
qualified.sort(key=lambda x: (x["cover_prob"], x["edge_value"], x["ev"]), reverse=True)
picks = qualified[:MAX_PICKS]

if len(picks) == 0:
    st.info("依挑場規則：前 10 場「Pinnacle 真盤」裡，沒有任何一場盤口優勢 > 5%。建議不買、不硬湊。")
else:
    if len(picks) == 1:
        st.success("🎯 今日只有 1 場符合門檻：建議只買單場（或分注單場），不要硬湊串關。")
    else:
        st.success(f"🎯 今日最能買：已依規則挑出 {len(picks)} 場（最多三場）。")

    cols = st.columns(len(picks))
    for idx, item in enumerate(picks):
        g = item["g"]
        with cols[idx]:
            with st.container(border=True):
                st.subheader(f"精選 {idx+1}")
                st.write(f"**{g['label']}**")
                st.success(f"首選：{item['pick_team']}")
                st.caption(f"盤口來源：{item['src']}（候選池=真盤；排序=你輸入的盤口/賠率）")

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
# 13) 結算按鈕：更新比分/cover 寫入 DB
# =========================================================
st.divider()
cA, cB = st.columns([0.55, 0.45])
with cA:
    if st.button("🧾 更新賽果 / 結算（Final 後寫入 cover）"):
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
                try:
                    upsert_game_row({
                        "game_id": gid,
                        "game_date_us": target_date_us,
                        "season": "2025-26",
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
                except Exception:
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
