import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats,
    leaguedashteamstats, leaguehustlestatteam, leaguedashptstats,
    synergyplaytypes, leaguedashptdefend
)
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import pytz, warnings
from datetime import datetime, timedelta

# =========================
# 1) 基本設定
# =========================
warnings.filterwarnings("ignore")

tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")  # NBA 官方時區

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

st.set_page_config(page_title="NBA 數據專家 v7.1", layout="wide")
st.title("🏀 NBA 數據專家 v7.1（加入 Inactive 排除 + 傷兵特徵）")

# =========================
# 2) 通用穩定抓取：支援指定 resultSet（關鍵：Scoreboard 的 InactivePlayers）
# =========================
def fetch_safe_df(endpoint_class, result_set_name=None, result_set_index=0, **kwargs):
    """
    支援從 NBA endpoint 取特定 resultSet：
    - result_set_name: 例如 'LineScore', 'GameHeader', 'InactivePlayers'
    - result_set_index: 若不指定名稱，取第幾個 resultSet
    """
    try:
        instance = endpoint_class(**kwargs)
        raw = instance.get_dict()

        # 常見：raw['resultSets'] 是 list
        if 'resultSets' in raw:
            rs_list = raw['resultSets']
            if result_set_name is not None:
                rs = next((x for x in rs_list if x.get('name') == result_set_name), None)
                if rs is None:
                    return pd.DataFrame()
            else:
                rs = rs_list[result_set_index]

        # 另一種：raw['resultSet'] 是 single dict
        else:
            rs = raw.get('resultSet', None)
            if rs is None:
                return pd.DataFrame()

        df = pd.DataFrame(rs['rowSet'], columns=rs['headers'])

        # 轉型保護
        for col in ["TEAM_ID", "HOME_TEAM_ID", "VISITOR_TEAM_ID", "PLAYER_ID", "GAME_ID"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # 常見 TEAM_ID 強制 int（避免 merge / dict key 問題）
        if "TEAM_ID" in df.columns:
            df["TEAM_ID"] = df["TEAM_ID"].fillna(0).astype(int)

        return df

    except Exception:
        return pd.DataFrame()


# =========================
# 3) 載入資料與訓練：加入「傷兵特徵」欄位（訓練期缺歷史傷兵→先用 0，推論期用當天 InactivePlayers）
# =========================
@st.cache_data(ttl=3600)
def load_all_data_v71():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S = "2025-26"
    ST = "Regular Season"

    # ---- Team level maps ----
    df_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv  = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_hustle = fetch_safe_df(leaguehustlestatteam.LeagueHustleStatsTeam, season=S, per_mode_time='PerGame')
    df_track_spd  = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
    df_track_pass = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='Passing', per_mode_simple='PerGame')
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
    df_iso   = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Isolation', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
    df_rim   = fetch_safe_df(leaguedashptdefend.LeagueDashPtDefend, season=S, defense_category='Less Than 6 Ft', season_type_all_star=ST)

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

    # ---- GameFinder for training ----
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    if gf_raw.empty:
        # 若 API 當下掛掉，回傳空，避免整個 app 崩
        dummy = pd.DataFrame()
        ps_full = pd.DataFrame()
        feats = []
        return None, None, dummy, ps_full, feats, maps, datetime.now(tw_tz).strftime("%H:%M")

    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()

    # 基本欄位整理
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)

    def get_v(tid, m, k, default=0):
        return maps.get(m, {}).get(int(tid), {}).get(k, default)

    # 建立 team feature
    gf['T_ORTG']  = gf['TEAM_ID'].apply(lambda x: get_v(x, 'adv', 'OFF_RATING', 110))
    gf['T_DRTG']  = gf['TEAM_ID'].apply(lambda x: get_v(x, 'adv', 'DEF_RATING', 110))
    gf['T_DEFL']  = gf['TEAM_ID'].apply(lambda x: get_v(x, 'hustle', 'DEFLECTIONS', 15))
    gf['T_TRANS'] = gf['TEAM_ID'].apply(lambda x: get_v(x, 'trans', 'PPP', 1.10))
    gf['T_RIM']   = gf['TEAM_ID'].apply(lambda x: get_v(x, 'rim', 'D_FG_PCT', 0.60))

    # ✅ 傷兵特徵（訓練期缺歷史傷兵名單 → 先全部設 0；推論期才用 scoreboard 的 InactivePlayers 填入）
    gf['STAR_OUT_COUNT'] = 0
    gf['STAR_OUT_PTS_SUM'] = 0.0

    # ---- 訓練特徵 ----
    feats = ['IS_HOME', 'REST_DAYS', 'T_ORTG', 'T_DRTG', 'T_DEFL', 'T_TRANS', 'T_RIM',
             'STAR_OUT_COUNT', 'STAR_OUT_PTS_SUM']

    train_df = gf.fillna(0)

    clf = xgb.XGBClassifier(
        n_estimators=300,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.9,
        colsample_bytree=0.9,
        reg_lambda=1.0,
        random_state=42,
        eval_metric="logloss"
    )
    clf.fit(train_df[feats], train_df['WIN_BIN'])

    reg = xgb.XGBRegressor(
        n_estimators=400,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.9,
        colsample_bytree=0.9,
        reg_lambda=1.0,
        random_state=42
    )
    # PLUS_MINUS 有時會是字串
    if 'PLUS_MINUS' in train_df.columns:
        train_df['PLUS_MINUS'] = pd.to_numeric(train_df['PLUS_MINUS'], errors='coerce').fillna(0)
    else:
        train_df['PLUS_MINUS'] = 0
    reg.fit(train_df[feats], train_df['PLUS_MINUS'])

    # ---- Player stats (Top 6 用) ----
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')

    if ps_raw.empty or ps_adv.empty:
        ps_full = pd.DataFrame()
    else:
        # 欄位保護
        keep_raw = [c for c in ['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST'] if c in ps_raw.columns]
        keep_adv = [c for c in ['PLAYER_ID', 'TS_PCT', 'PIE'] if c in ps_adv.columns]
        ps_full = pd.merge(ps_raw[keep_raw], ps_adv[keep_adv], on='PLAYER_ID', how='inner')

        # 轉型
        for col in ['PTS', 'REB', 'AST', 'TS_PCT', 'PIE']:
            if col in ps_full.columns:
                ps_full[col] = pd.to_numeric(ps_full[col], errors='coerce')

    last_update = datetime.now(tw_tz).strftime("%H:%M")
    return clf, reg, gf, ps_full, feats, maps, last_update


clf, reg, gf, ps_full, feats, maps, last_update = load_all_data_v71()

if clf is None or reg is None or gf is None or gf.empty:
    st.error("目前 NBA API 資料抓取失敗（GameFinder 取不到）。請稍後重整。")
    st.stop()

# =========================
# 4) 工具：從當天 InactivePlayers 建立 team->inactive map + 算「主力缺陣特徵」
# =========================
def build_inactive_maps(game_date_mmddyyyy: str):
    """
    用 ScoreboardV2 的 InactivePlayers 取得當天缺陣名單
    回傳：
      inactive_ids_map:  team_id -> set(player_id)
      inactive_names_map: team_id -> list(player_name)
    """
    inactive_df = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=game_date_mmddyyyy, result_set_name='InactivePlayers')
    inactive_ids_map, inactive_names_map = {}, {}

    if inactive_df.empty or 'TEAM_ID' not in inactive_df.columns:
        return inactive_ids_map, inactive_names_map

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

    return inactive_ids_map, inactive_names_map


def calc_star_out_features(team_id: int, inactive_ids: set, ps_full_df: pd.DataFrame, top_n=5):
    """
    以 ps_full 的 PTS 由高到低選 Top-N 主力，
    計算：
      STAR_OUT_COUNT = 其中有幾個在 inactive
      STAR_OUT_PTS_SUM = inactive 主力的場均得分加總（粗略量化戰力損失）
    """
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


def get_m(m, tid, k, default=0):
    return maps.get(m, {}).get(int(tid), {}).get(k, default)


# =========================
# 5) 介面顯示（時差修正 + 用 LineScore 拿對戰 + 用 InactivePlayers 排除報銷/缺陣）
# =========================
nba_now = datetime.now(us_east_tz)
dates_nba = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1), nba_now - timedelta(days=2)]
tab_titles = [d.astimezone(tw_tz).strftime('%m/%d') for d in dates_nba]
tabs = st.tabs(tab_titles)

# team id -> abbr
id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}

for i, tab in enumerate(tabs):
    with tab:
        search_date = dates_nba[i].strftime('%m/%d/%Y')  # Scoreboard 要美式
        line_score = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=search_date, result_set_name='LineScore')

        # 當天缺陣
        inactive_ids_map, inactive_names_map = build_inactive_maps(search_date)

        if line_score.empty:
            st.info(f"📅 美國時間 {dates_nba[i].strftime('%Y-%m-%d')} 暫無賽程數據")
            continue

        games = {}

        for _, row in line_score.iterrows():
            h_id = int(row['HOME_TEAM_ID'])
            a_id = int(row['VISITOR_TEAM_ID'])
            h_abbr = id_to_abbr.get(h_id)
            a_abbr = id_to_abbr.get(a_id)
            if not h_abbr or not a_abbr:
                continue

            # 用主隊「上一場」當作狀態特徵（你原本做法）
            h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
            if h_last.empty:
                continue

            # 先準備 base feature row
            base_x = h_last[feats].copy()

            # ✅ 推論期填入傷兵特徵（主/客都算，放進同一筆 feature 會不對）
            # 你的模型目前只有一隊視角 (用 h_last) → 先用「主隊傷兵」填入，並用 heuristic 顯示客隊傷兵影響
            h_inactive_ids = inactive_ids_map.get(h_id, set())
            a_inactive_ids = inactive_ids_map.get(a_id, set())

            h_out_count, h_out_pts = calc_star_out_features(h_id, h_inactive_ids, ps_full, top_n=5)
            a_out_count, a_out_pts = calc_star_out_features(a_id, a_inactive_ids, ps_full, top_n=5)

            # 模型欄位：先填主隊（因為你用的是主隊 last-game row）
            if 'STAR_OUT_COUNT' in base_x.columns:
                base_x.loc[:, 'STAR_OUT_COUNT'] = h_out_count
            if 'STAR_OUT_PTS_SUM' in base_x.columns:
                base_x.loc[:, 'STAR_OUT_PTS_SUM'] = h_out_pts

            # 預測
            prob_home = float(clf.predict_proba(base_x)[0][1]) * 100.0
            diff = round(abs(float(reg.predict(base_x)[0])), 1)

            # ✅ 客隊傷兵用簡單修正（避免你目前特徵設計只有單隊視角導致「客隊傷兵」完全沒被反映）
            # 規則：客隊 Top5 主力缺陣每 1 人，主隊勝率 +2%；每缺 1 分場均得分，主隊勝率 +0.3%（上限做 clamp）
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
            st.info("查到賽程，但找不到可用的對戰資料（可能 GameFinder/球隊對照暫時缺）。")
            continue

        selected = st.selectbox("🎯 選擇分析場次", list(games.keys()), key=f"s_{i}")
        res = games[selected]

        st.markdown(f"### 🏟️ {selected}")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric(res['h_name'], f"{res['prob_home']:.1f}%")
        c2.metric(res['a_name'], f"{100 - res['prob_home']:.1f}%")
        c3.metric("AI 預測贏家", res['winner'], f"分差預測: {res['diff']} 分")
        c4.metric("傷兵修正", f"+{(res['prob_home'] - (res['prob_home'] - min(12.0, res['a_out_count'] * 2.0 + res['a_out_pts'] * 0.3))):.1f}%",
                  "（依客隊缺主力粗略加權）")

        # ✅ 顯示缺陣名單（當天）
        h_inactive_names = inactive_names_map.get(res['h_id'], [])
        a_inactive_names = inactive_names_map.get(res['a_id'], [])

        if h_inactive_names or a_inactive_names:
            st.subheader("🚑 本場缺陣/不出賽名單（Scoreboard InactivePlayers）")
            colA, colB = st.columns(2)
            with colA:
                st.caption(f"🏠 {res['h_name']}：主力缺陣（Top5）{res['h_out_count']} 人，缺陣主力場均得分合計 {res['h_out_pts']:.1f}")
                if h_inactive_names:
                    st.write("、".join(h_inactive_names[:20]) + ("..." if len(h_inactive_names) > 20 else ""))
                else:
                    st.write("（無資料/無缺陣）")
            with colB:
                st.caption(f"✈️ {res['a_name']}：主力缺陣（Top5）{res['a_out_count']} 人，缺陣主力場均得分合計 {res['a_out_pts']:.1f}")
                if a_inactive_names:
                    st.write("、".join(a_inactive_names[:20]) + ("..." if len(a_inactive_names) > 20 else ""))
                else:
                    st.write("（無資料/無缺陣）")

        # =========================
        # 團隊數據表
        # =========================
        st.subheader("📊 1. 團隊場均基礎數據")
        st.table(pd.DataFrame({
            "指標項目": ["場均得分", "場均籃板", "場均助攻", "團隊命中率",
                     "進攻效率 (OffRtg)", "防守效率 (DefRtg)", "比賽節奏 (Pace)"],
            res['h_name']: [
                f"{get_m('base', res['h_id'], 'PTS'):.1f} 分",
                f"{get_m('base', res['h_id'], 'REB'):.1f} 個",
                f"{get_m('base', res['h_id'], 'AST'):.1f} 次",
                f"{get_m('base', res['h_id'], 'FG_PCT'):.1%}",
                f"{get_m('adv', res['h_id'], 'OFF_RATING')} pts/100",
                f"{get_m('adv', res['h_id'], 'DEF_RATING')} pts/100",
                f"{get_m('adv', res['h_id'], 'PACE')} 次"
            ],
            res['a_name']: [
                f"{get_m('base', res['a_id'], 'PTS'):.1f} 分",
                f"{get_m('base', res['a_id'], 'REB'):.1f} 個",
                f"{get_m('base', res['a_id'], 'AST'):.1f} 次",
                f"{get_m('base', res['a_id'], 'FG_PCT'):.1%}",
                f"{get_m('adv', res['a_id'], 'OFF_RATING')} pts/100",
                f"{get_m('adv', res['a_id'], 'DEF_RATING')} pts/100",
                f"{get_m('adv', res['a_id'], 'PACE')} 次"
            ]
        }))

        st.subheader("🏃‍♂️ 2. 體能與積極度追蹤（場均）")
        st.table(pd.DataFrame({
            "追蹤指標": ["撥球破壞 (Deflections)", "干擾投籃 (Contested)",
                     "場均跑動里程", "平均移動速度", "場均傳球次數"],
            res['h_name']: [
                f"{get_m('hustle', res['h_id'], 'DEFLECTIONS'):.1f} 次",
                f"{get_m('hustle', res['h_id'], 'CONTESTED_SHOTS'):.1f} 次",
                f"{get_m('spd', res['h_id'], 'DIST_MILES'):.2f} mi",
                f"{get_m('spd', res['h_id'], 'AVG_SPEED'):.2f} mph",
                f"{get_m('pass', res['h_id'], 'PASSES_MADE'):.1f} 次"
            ],
            res['a_name']: [
                f"{get_m('hustle', res['a_id'], 'DEFLECTIONS'):.1f} 次",
                f"{get_m('hustle', res['a_id'], 'CONTESTED_SHOTS'):.1f} 次",
                f"{get_m('spd', res['a_id'], 'DIST_MILES'):.2f} mi",
                f"{get_m('spd', res['a_id'], 'AVG_SPEED'):.2f} mph",
                f"{get_m('pass', res['a_id'], 'PASSES_MADE'):.1f} 次"
            ]
        }))

        st.subheader("⚔️ 3. 戰術類別與護框效率")
        st.table(pd.DataFrame({
            "戰術/防守指標": ["轉換進攻效率 (PPP)", "單打得分效率 (PPP)", "籃框護框命中率 (D-FG%)"],
            res['h_name']: [
                f"{get_m('trans', res['h_id'], 'PPP'):.2f}",
                f"{get_m('iso', res['h_id'], 'PPP'):.2f}",
                f"{get_m('rim', res['h_id'], 'D_FG_PCT'):.1%}"
            ],
            res['a_name']: [
                f"{get_m('trans', res['a_id'], 'PPP'):.2f}",
                f"{get_m('iso', res['a_id'], 'PPP'):.2f}",
                f"{get_m('rim', res['a_id'], 'D_FG_PCT'):.1%}"
            ]
        }))

        # =========================
        # ✅ 核心球員 Top6：排除當天 InactivePlayers（你要修的重點）
        # =========================
        st.subheader("🚀 4. 核心球員傳統數據（Top 6；已排除本場缺陣）")

        if ps_full is None or ps_full.empty:
            st.warning("球員資料抓取失敗（LeagueDashPlayerStats）。")
        else:
            for tid, label in [(res['h_id'], f"🏠 {res['h_name']}"), (res['a_id'], f"✈️ {res['a_name']}")]:
                st.write(f"**{label}**")

                inactive_ids = inactive_ids_map.get(int(tid), set())
                inactive_names = inactive_names_map.get(int(tid), [])

                p_df_all = ps_full[ps_full['TEAM_ID'] == int(tid)].copy()

                # 排除當天確定不出賽/缺陣球員
                if 'PLAYER_ID' in p_df_all.columns and len(inactive_ids) > 0:
                    p_df_all = p_df_all[~p_df_all['PLAYER_ID'].astype(int).isin(inactive_ids)]

                p_df = p_df_all.sort_values('PTS', ascending=False).head(6)

                if inactive_names:
                    st.caption("🚑 本場缺陣/不出賽： " + "、".join(inactive_names[:12]) + ("..." if len(inactive_names) > 12 else ""))

                show_cols = []
                for c in ['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT', 'PIE']:
                    if c in p_df.columns:
                        show_cols.append(c)

                if p_df.empty:
                    st.info("（本隊可用球員資料不足，或皆被標示缺陣）")
                else:
                    df_show = p_df[show_cols].rename(columns={
                        'PLAYER_NAME': '姓名', 'PTS': '得分', 'REB': '籃板', 'AST': '助攻', 'TS_PCT': '真實命中%', 'PIE': 'PIE'
                    })
                    fmt = {}
                    if '得分' in df_show.columns: fmt['得分'] = '{:.1f}'
                    if '籃板' in df_show.columns: fmt['籃板'] = '{:.1f}'
                    if '助攻' in df_show.columns: fmt['助攻'] = '{:.1f}'
                    if '真實命中%' in df_show.columns: fmt['真實命中%'] = '{:.1%}'
                    if 'PIE' in df_show.columns: fmt['PIE'] = '{:.3f}'

                    st.dataframe(df_show.style.format(fmt), hide_index=True)

st.sidebar.caption(f"🕒 更新時間：{last_update}（台北）")

st.sidebar.markdown("---")
st.sidebar.caption(
    "備註：\n"
    "1) 本版已用 ScoreboardV2 的 InactivePlayers 排除缺陣球員（Top6 不會再把整季報銷算進去）。\n"
    "2) 傷兵特徵已加入模型欄位，但訓練期缺歷史傷兵資料→訓練時為 0；推論時以當天缺陣名單填入。\n"
    "3) 因你目前預測採『主隊單隊視角』，客隊傷兵用 heuristic 修正勝率（顯示在「傷兵修正」）。\n"
    "   若你要把模型改成真正『主客雙隊特徵』(差值/比值)，我也可以幫你重構。"
)
