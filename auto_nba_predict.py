# auto_nba_predict.py
# NBA 數據專家 v7.3
# - 兼容 nba_api endpoints 命名差異（避免 ImportError）
# - Scoreboard InactivePlayers：排除缺陣/報銷球員（Top6 不再把整季報銷算進去）
# - 加入「主力缺陣特徵」(STAR_OUT_COUNT / STAR_OUT_PTS_SUM) 用於推論（訓練期無歷史傷兵→訓練置 0）
# - 雲端穩定性：加 NBA Stats headers + retry/backoff + timeout
# - 降低 API 壓力：加值資料（hustle/tracking/synergy/ptdefend）抓不到就空，不讓 app 崩

import streamlit as st
import pandas as pd
import xgboost as xgb
import pytz, warnings
from datetime import datetime, timedelta
import importlib
import time
import random

from nba_api.stats.static import teams

# =========================
# 0) Streamlit 基本設定
# =========================
warnings.filterwarnings("ignore")
tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")

TEAM_NAME_CH = {
    'ATL': '亞特蘭大老鷹', 'BKN': '布魯克林籃網', 'BOS': '波士頓塞爾提克',
    'CHA': '夏洛特黃蜂', 'CHI': '芝加哥公牛', 'CLE': '克里夫蘭騎士',
    'DAL': '達拉斯獨行俠', 'DEN': '丹佛金塊', 'DET': '底特律活塞',
    'GSW': '金州勇士', 'HOU': '休士頓火箭', 'IND': '印第安納溜馬',
    'LAC': '洛杉磯快艇', 'LAL': '洛杉磯湖人', 'MEM': '曼非斯灰熊',
    'MIA': '邁阿密熱火', 'MIL': '密爾瓦基公鹿', 'MIN': '明尼蘇達灰狼',
    'NOP': '紐奧良鵜鶘', 'NYK': '紐約尼克', 'OKC': '奧克拉荷馬雷霆',
    'ORL': '奧蘭多魔術', 'PHI': '費城 76 人', 'PHX': '鳳凰城太陽',
    'POR': '波特蘭開拓者', 'SAC': '沙加緬度國王', 'SAS': '聖安東尼奧馬刺',
    'TOR': '多倫多暴龍', 'UTA': '猶他爵士', 'WAS': '華盛頓巫師'
}

st.set_page_config(page_title="NBA 數據專家 v7.3", layout="wide")
st.title("🏀 NBA 數據專家 v7.3（雲端穩定抓取 + Inactive 排除 + 主力缺陣特徵）")

# =========================
# 1) 動態匯入 endpoints（避免版本差異直接 ImportError）
# =========================
def import_endpoint(module_name: str):
    try:
        return importlib.import_module(f"nba_api.stats.endpoints.{module_name}")
    except Exception:
        return None

leaguegamefinder = import_endpoint("leaguegamefinder")
scoreboardv2 = import_endpoint("scoreboardv2")
leaguedashplayerstats = import_endpoint("leaguedashplayerstats")
leaguedashteamstats = import_endpoint("leaguedashteamstats")
leaguedashptstats = import_endpoint("leaguedashptstats")
synergyplaytypes = import_endpoint("synergyplaytypes")
leaguedashptdefend = import_endpoint("leaguedashptdefend")

# hustle team endpoint：不同版本可能不同名稱
leaguehustlestatteam = import_endpoint("leaguehustlestatteam")
leaguehustlestats = import_endpoint("leaguehustlestats")

# 必要 endpoints 檢查
required = [
    (leaguegamefinder, "leaguegamefinder"),
    (scoreboardv2, "scoreboardv2"),
    (leaguedashplayerstats, "leaguedashplayerstats"),
    (leaguedashteamstats, "leaguedashteamstats"),
]
missing = [name for mod, name in required if mod is None]
if missing:
    st.error("你的 nba_api 版本缺少必要 endpoints 模組： " + ", ".join(missing))
    st.stop()

# =========================
# 2) NBA Stats 雲端 headers + retry/backoff
# =========================
NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "Connection": "keep-alive",
}

def fetch_safe_df(endpoint_class, result_set_name=None, result_set_index=0,
                  max_retries=5, base_sleep=1.2, timeout=30, **kwargs):
    """
    - 自動帶 NBA_HEADERS（避免雲端被 NBA Stats 擋）
    - 失敗重試（指數退避 + 抖動）
    - 回傳 (df, err)；成功 err=None
    """
    last_err = None
    for attempt in range(max_retries):
        try:
            instance = endpoint_class(headers=NBA_HEADERS, timeout=timeout, **kwargs)
            raw = instance.get_dict()

            if 'resultSets' in raw:
                rs_list = raw['resultSets']
                if result_set_name is not None:
                    rs = next((x for x in rs_list if x.get('name') == result_set_name), None)
                    if rs is None:
                        return pd.DataFrame(), f"result_set_name '{result_set_name}' not found"
                else:
                    rs = rs_list[result_set_index]
            else:
                rs = raw.get('resultSet', None)
                if rs is None:
                    return pd.DataFrame(), "resultSet not found"

            df = pd.DataFrame(rs['rowSet'], columns=rs['headers'])

            for col in ["TEAM_ID", "HOME_TEAM_ID", "VISITOR_TEAM_ID", "PLAYER_ID", "GAME_ID"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            if "TEAM_ID" in df.columns:
                df["TEAM_ID"] = df["TEAM_ID"].fillna(0).astype(int)

            return df, None

        except Exception as e:
            last_err = str(e)
            sleep_s = base_sleep * (2 ** attempt) + random.uniform(0, 0.6)
            time.sleep(sleep_s)

    return pd.DataFrame(), last_err

# =========================
# 3) Hustle team endpoint 兼容抓取
# =========================
def fetch_hustle_team_df(season: str):
    # 你的原本名稱
    if leaguehustlestatteam is not None and hasattr(leaguehustlestatteam, "LeagueHustleStatsTeam"):
        df, _ = fetch_safe_df(
            leaguehustlestatteam.LeagueHustleStatsTeam,
            season=season,
            per_mode_time="PerGame"
        )
        return df
    # 備援名稱
    if leaguehustlestats is not None and hasattr(leaguehustlestats, "LeagueHustleStatsTeam"):
        df, _ = fetch_safe_df(
            leaguehustlestats.LeagueHustleStatsTeam,
            season=season,
            per_mode_time="PerGame"
        )
        return df
    return pd.DataFrame()

# =========================
# 4) 資料載入與模型訓練（cache）
# =========================
@st.cache_data(ttl=3600)
def load_all_data_v73():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S = "2025-26"
    ST = "Regular Season"

    # ---- team stats（必抓）----
    df_base, err_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv, err_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')

    # ---- optional stats（抓不到也不崩）----
    df_hustle = fetch_hustle_team_df(S)

    df_track_spd = pd.DataFrame()
    df_track_pass = pd.DataFrame()
    if leaguedashptstats is not None:
        df_track_spd, _ = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
        df_track_pass, _ = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='Passing', per_mode_simple='PerGame')

    df_trans = pd.DataFrame()
    df_iso = pd.DataFrame()
    if synergyplaytypes is not None:
        df_trans, _ = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
        df_iso, _ = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Isolation', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)

    df_rim = pd.DataFrame()
    if leaguedashptdefend is not None:
        df_rim, _ = fetch_safe_df(leaguedashptdefend.LeagueDashPtDefend, season=S, defense_category='Less Than 6 Ft', season_type_all_star=ST)

    def to_map(df, cols):
        if df.empty or "TEAM_ID" not in df.columns:
            return {}
        keep = [c for c in cols if c in df.columns]
        if not keep:
            return {}
        return df.set_index("TEAM_ID")[keep].to_dict("index")

    maps = {
        'base': to_map(df_base, ['PTS', 'REB', 'AST', 'FG_PCT']),
        'adv':  to_map(df_adv,  ['OFF_RATING', 'DEF_RATING', 'PACE']),
        'hustle': to_map(df_hustle, ['DEFLECTIONS', 'CONTESTED_SHOTS']),
        'spd': to_map(df_track_spd, ['DIST_MILES', 'AVG_SPEED']),
        'pass': to_map(df_track_pass, ['PASSES_MADE']),
        'trans': to_map(df_trans, ['PPP']),
        'iso':   to_map(df_iso,   ['PPP']),
        'rim':   to_map(df_rim,   ['D_FG_PCT'])
    }

    # ---- GameFinder（必抓，會影響模型訓練）----
    gf_raw, gf_err = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S, max_retries=6, timeout=40)
    if gf_raw.empty:
        return None, None, pd.DataFrame(), pd.DataFrame(), [], maps, datetime.now(tw_tz).strftime("%H:%M"), gf_err, (err_base or err_adv)

    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)

    def get_v(tid, m, k, default=0):
        return maps.get(m, {}).get(int(tid), {}).get(k, default)

    gf['T_ORTG']  = gf['TEAM_ID'].apply(lambda x: get_v(x, 'adv', 'OFF_RATING', 110))
    gf['T_DRTG']  = gf['TEAM_ID'].apply(lambda x: get_v(x, 'adv', 'DEF_RATING', 110))
    gf['T_DEFL']  = gf['TEAM_ID'].apply(lambda x: get_v(x, 'hustle', 'DEFLECTIONS', 15))
    gf['T_TRANS'] = gf['TEAM_ID'].apply(lambda x: get_v(x, 'trans', 'PPP', 1.10))
    gf['T_RIM']   = gf['TEAM_ID'].apply(lambda x: get_v(x, 'rim', 'D_FG_PCT', 0.60))

    # 訓練期沒有歷史傷兵 → 0
    gf['STAR_OUT_COUNT'] = 0
    gf['STAR_OUT_PTS_SUM'] = 0.0

    feats = ['IS_HOME', 'REST_DAYS', 'T_ORTG', 'T_DRTG', 'T_DEFL', 'T_TRANS', 'T_RIM',
             'STAR_OUT_COUNT', 'STAR_OUT_PTS_SUM']

    train_df = gf.fillna(0)
    train_df['PLUS_MINUS'] = pd.to_numeric(train_df.get('PLUS_MINUS', 0), errors='coerce').fillna(0)

    clf = xgb.XGBClassifier(
        n_estimators=300, max_depth=4, learning_rate=0.05,
        subsample=0.9, colsample_bytree=0.9,
        reg_lambda=1.0, random_state=42, eval_metric="logloss"
    )
    clf.fit(train_df[feats], train_df['WIN_BIN'])

    reg = xgb.XGBRegressor(
        n_estimators=400, max_depth=4, learning_rate=0.05,
        subsample=0.9, colsample_bytree=0.9,
        reg_lambda=1.0, random_state=42
    )
    reg.fit(train_df[feats], train_df['PLUS_MINUS'])

    # ---- Player stats（Top6 用）----
    ps_raw, ps_raw_err = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv, ps_adv_err = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')

    if ps_raw.empty or ps_adv.empty:
        ps_full = pd.DataFrame()
    else:
        keep_raw = [c for c in ['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST'] if c in ps_raw.columns]
        keep_adv = [c for c in ['PLAYER_ID', 'TS_PCT', 'PIE'] if c in ps_adv.columns]
        ps_full = pd.merge(ps_raw[keep_raw], ps_adv[keep_adv], on='PLAYER_ID', how='inner')
        for col in ['PTS', 'REB', 'AST', 'TS_PCT', 'PIE']:
            if col in ps_full.columns:
                ps_full[col] = pd.to_numeric(ps_full[col], errors='coerce')

    last_update = datetime.now(tw_tz).strftime("%H:%M")
    return clf, reg, gf, ps_full, feats, maps, last_update, None, (err_base or err_adv)

# =========================
# 5) Scoreboard InactivePlayers：建立 team->inactive map
# =========================
def build_inactive_maps(game_date_mmddyyyy: str):
    inactive_ids_map, inactive_names_map = {}, {}
    inactive_df, err = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=game_date_mmddyyyy, result_set_name='InactivePlayers', max_retries=5, timeout=30)

    if inactive_df.empty or 'TEAM_ID' not in inactive_df.columns:
        return inactive_ids_map, inactive_names_map, err

    pid_col = 'PLAYER_ID' if 'PLAYER_ID' in inactive_df.columns else None
    pname_col = 'PLAYER_NAME' if 'PLAYER_NAME' in inactive_df.columns else None

    for tid, g in inactive_df.groupby('TEAM_ID'):
        tid = int(tid)
        if pid_col:
            ids = pd.to_numeric(g[pid_col], errors='coerce').dropna().astype(int).tolist()
            inactive_ids_map[tid] = set(ids)
        else:
            inactive_ids_map[tid] = set()

        if pname_col:
            inactive_names_map[tid] = g[pname_col].dropna().astype(str).tolist()
        else:
            inactive_names_map[tid] = []

    return inactive_ids_map, inactive_names_map, None

def calc_star_out_features(team_id: int, inactive_ids: set, ps_full_df: pd.DataFrame, top_n=5):
    if ps_full_df is None or ps_full_df.empty or 'PLAYER_ID' not in ps_full_df.columns or 'PTS' not in ps_full_df.columns:
        return 0, 0.0

    roster = ps_full_df[ps_full_df['TEAM_ID'] == int(team_id)].copy()
    roster = roster.dropna(subset=['PLAYER_ID', 'PTS'])
    if roster.empty:
        return 0, 0.0

    top = roster.sort_values('PTS', ascending=False).head(top_n)
    top_ids = set(top['PLAYER_ID'].astype(int).tolist())
    out_ids = top_ids.intersection(set(inactive_ids))

    out_count = len(out_ids)
    out_pts_sum = float(top[top['PLAYER_ID'].astype(int).isin(out_ids)]['PTS'].sum()) if out_count > 0 else 0.0
    return out_count, out_pts_sum

def get_m(maps, m, tid, k, default=0):
    return maps.get(m, {}).get(int(tid), {}).get(k, default)

# =========================
# 6) 主程式：載入資料
# =========================
clf, reg, gf, ps_full, feats, maps, last_update, gf_err, teamstats_err = load_all_data_v73()

if gf is None or gf.empty or clf is None or reg is None:
    st.error("目前 NBA API 資料抓取失敗（GameFinder 取不到）。")
    if gf_err:
        st.caption(f"GameFinder error: {gf_err}")
        st.caption("多半是 403/429/timeout：雲端被 NBA Stats 擋或限流。已加入 headers+retry，若仍失敗請稍後重整。")
    st.stop()

# Sidebar debug（可關掉）
with st.sidebar:
    st.caption(f"🕒 更新時間：{last_update}（台北）")
    if teamstats_err:
        st.caption(f"TeamStats warning: {teamstats_err}")

id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}

# =========================
# 7) 介面：時差修正 + 使用 LineScore 對戰 + Inactive 排除
# =========================
nba_now = datetime.now(us_east_tz)
dates_nba = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1), nba_now - timedelta(days=2)]
tab_titles = [d.astimezone(tw_tz).strftime('%m/%d') for d in dates_nba]
tabs = st.tabs(tab_titles)

for i, tab in enumerate(tabs):
    with tab:
        search_date = dates_nba[i].strftime('%m/%d/%Y')

        # LineScore（用來列對戰）
        line_score, ls_err = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=search_date, result_set_name='LineScore', max_retries=5, timeout=30)

        # InactivePlayers（用來排除缺陣）
        inactive_ids_map, inactive_names_map, inact_err = build_inactive_maps(search_date)

        if line_score.empty:
            st.info(f"📅 美國時間 {dates_nba[i].strftime('%Y-%m-%d')} 暫無賽程數據")
            if ls_err:
                st.caption(f"LineScore error: {ls_err}")
            continue

        games = {}
        for _, row in line_score.iterrows():
            h_id = int(row['HOME_TEAM_ID'])
            a_id = int(row['VISITOR_TEAM_ID'])

            h_abbr = id_to_abbr.get(h_id)
            a_abbr = id_to_abbr.get(a_id)
            if not h_abbr or not a_abbr:
                continue

            # 你原本做法：用主隊上一場作為 feature
            h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
            if h_last.empty:
                continue

            base_x = h_last[feats].copy()

            h_inactive_ids = inactive_ids_map.get(h_id, set())
            a_inactive_ids = inactive_ids_map.get(a_id, set())

            h_out_count, h_out_pts = calc_star_out_features(h_id, h_inactive_ids, ps_full, top_n=5)
            a_out_count, a_out_pts = calc_star_out_features(a_id, a_inactive_ids, ps_full, top_n=5)

            # 模型欄位：填主隊傷兵特徵
            base_x.loc[:, 'STAR_OUT_COUNT'] = h_out_count
            base_x.loc[:, 'STAR_OUT_PTS_SUM'] = h_out_pts

            prob_home = float(clf.predict_proba(base_x)[0][1]) * 100.0
            diff = round(abs(float(reg.predict(base_x)[0])), 1)

            # 客隊傷兵 heuristic 修正（因為模型目前是「主隊單隊視角」）
            # Top5 主力每缺 1 人 → +2%，缺分每 1 分 → +0.3%，上限 12%
            adj = min(12.0, a_out_count * 2.0 + a_out_pts * 0.3)
            prob_home_adj = max(1.0, min(99.0, prob_home + adj))

            winner_abbr = h_abbr if prob_home_adj >= 50 else a_abbr

            games[f"{TEAM_NAME_CH.get(a_abbr, a_abbr)} @ {TEAM_NAME_CH.get(h_abbr, h_abbr)}"] = {
                'h_id': h_id, 'a_id': a_id,
                'h_abbr': h_abbr, 'a_abbr': a_abbr,
                'h_name': TEAM_NAME_CH.get(h_abbr, h_abbr),
                'a_name': TEAM_NAME_CH.get(a_abbr, a_abbr),
                'prob_home': prob_home_adj,
                'diff': diff,
                'winner': TEAM_NAME_CH.get(winner_abbr, winner_abbr),
                'h_out_count': h_out_count, 'h_out_pts': h_out_pts,
                'a_out_count': a_out_count, 'a_out_pts': a_out_pts
            }

        if not games:
            st.info("查到賽程，但找不到可用的對戰資料。")
            continue

        selected = st.selectbox("🎯 選擇分析場次", list(games.keys()), key=f"s_{i}")
        res = games[selected]

        st.markdown(f"### 🏟️ {selected}")

        c1, c2, c3 = st.columns(3)
        c1.metric(res['h_name'], f"{res['prob_home']:.1f}%")
        c2.metric(res['a_name'], f"{100 - res['prob_home']:.1f}%")
        c3.metric("AI 預測贏家", res['winner'], f"分差預測: {res['diff']} 分")

        # 缺陣名單顯示
        h_inactive_names = inactive_names_map.get(res['h_id'], [])
        a_inactive_names = inactive_names_map.get(res['a_id'], [])

        if h_inactive_names or a_inactive_names:
            st.subheader("🚑 本場缺陣/不出賽名單（Scoreboard InactivePlayers）")
            colA, colB = st.columns(2)
            with colA:
                st.caption(f"🏠 {res['h_name']}：Top5 主力缺陣 {res['h_out_count']} 人 / 缺分 {res['h_out_pts']:.1f}")
                st.write("、".join(h_inactive_names[:20]) + ("..." if len(h_inactive_names) > 20 else "") or "（無資料/無缺陣）")
            with colB:
                st.caption(f"✈️ {res['a_name']}：Top5 主力缺陣 {res['a_out_count']} 人 / 缺分 {res['a_out_pts']:.1f}")
                st.write("、".join(a_inactive_names[:20]) + ("..." if len(a_inactive_names) > 20 else "") or "（無資料/無缺陣）")
        else:
            if inact_err:
                st.caption(f"InactivePlayers error: {inact_err}（可能被限流/擋）")

        # 團隊數據表
        st.subheader("📊 1. 團隊場均基礎數據")
        st.table(pd.DataFrame({
            "指標項目": ["場均得分", "場均籃板", "場均助攻", "團隊命中率",
                     "進攻效率 (OffRtg)", "防守效率 (DefRtg)", "比賽節奏 (Pace)"],
            res['h_name']: [
                f"{get_m(maps,'base', res['h_id'], 'PTS'):.1f} 分",
                f"{get_m(maps,'base', res['h_id'], 'REB'):.1f} 個",
                f"{get_m(maps,'base', res['h_id'], 'AST'):.1f} 次",
                f"{get_m(maps,'base', res['h_id'], 'FG_PCT'):.1%}",
                f"{get_m(maps,'adv', res['h_id'], 'OFF_RATING', 0)} pts/100",
                f"{get_m(maps,'adv', res['h_id'], 'DEF_RATING', 0)} pts/100",
                f"{get_m(maps,'adv', res['h_id'], 'PACE', 0)} 次"
            ],
            res['a_name']: [
                f"{get_m(maps,'base', res['a_id'], 'PTS'):.1f} 分",
                f"{get_m(maps,'base', res['a_id'], 'REB'):.1f} 個",
                f"{get_m(maps,'base', res['a_id'], 'AST'):.1f} 次",
                f"{get_m(maps,'base', res['a_id'], 'FG_PCT'):.1%}",
                f"{get_m(maps,'adv', res['a_id'], 'OFF_RATING', 0)} pts/100",
                f"{get_m(maps,'adv', res['a_id'], 'DEF_RATING', 0)} pts/100",
                f"{get_m(maps,'adv', res['a_id'], 'PACE', 0)} 次"
            ]
        }))

        # Top6：排除當天 inactive（你的核心需求）
        st.subheader("🚀 2. 核心球員數據（Top 6；已排除本場缺陣）")
        if ps_full is None or ps_full.empty:
            st.warning("球員資料抓取失敗（LeagueDashPlayerStats）。")
        else:
            for tid, label in [(res['h_id'], f"🏠 {res['h_name']}"), (res['a_id'], f"✈️ {res['a_name']}")]:
                st.write(f"**{label}**")
                inactive_ids = inactive_ids_map.get(int(tid), set())

                p_df_all = ps_full[ps_full['TEAM_ID'] == int(tid)].copy()
                if 'PLAYER_ID' in p_df_all.columns and inactive_ids:
                    p_df_all = p_df_all[~p_df_all['PLAYER_ID'].astype(int).isin(inactive_ids)]

                p_df = p_df_all.sort_values('PTS', ascending=False).head(6)

                if p_df.empty:
                    st.info("（本隊可用球員資料不足，或皆被標示缺陣）")
                    continue

                df_show = p_df[['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT', 'PIE']].rename(columns={
                    'PLAYER_NAME': '姓名', 'PTS': '得分', 'REB': '籃板', 'AST': '助攻', 'TS_PCT': '真實命中%', 'PIE': 'PIE'
                })
                st.dataframe(df_show.style.format({
                    '得分': '{:.1f}',
                    '籃板': '{:.1f}',
                    '助攻': '{:.1f}',
                    '真實命中%': '{:.1%}',
                    'PIE': '{:.3f}'
                }), hide_index=True)

# =========================
# 8) sidebar 備註
# =========================
st.sidebar.markdown("---")
st.sidebar.caption(
    "備註：\n"
    "1) InactivePlayers 由 ScoreboardV2 提供，Top6 會排除缺陣/報銷球員。\n"
    "2) 已加入 headers + retry/backoff，雲端較不容易被 403/429 擋。\n"
    "3) 你的模型仍是「主隊單隊視角」：客隊傷兵用 heuristic 修正勝率；若要改成真正主客雙隊特徵（差值/比值）我可以再幫你重構。"
)
