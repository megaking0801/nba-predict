import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    synergyplaytypes, leaguedashptdefend
)
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import pytz
from datetime import datetime, timedelta
import time

# --- 1. 基本設定 ---
tw_tz = pytz.timezone('Asia/Taipei')

TEAM_NAME_CH = {
    'ATL': '亞特蘭大老鷹', 'BKN': '布魯克林籃網', 'BOS': '波士頓塞爾提克',
    'CHA': '夏洛特黃蜂', 'CHI': '芝加哥公牛', 'CLE': '克里夫蘭騎士',
    'DAL': '達拉斯獨行俠', 'DEN': '丹佛金塊', 'DET': '底特律活塞',
    'GSW': '金州勇士', 'HOU': '休士頓火箭', 'IND': '印第安納溜馬',
    'LAC': '洛杉磯快艇', 'LAL': '洛杉磯湖人', 'MEM': '曼非斯灰熊',
    'MIA': '邁阿密熱火', 'MIL': '密爾瓦基公鹿', 'MIN': '明尼蘇達灰狼',
    'NOP': '紐奧良鵜鶘', 'NYK': '紐約尼克', 'OKC': '奧克拉荷馬雷霆',
    'ORL': '奧蘭多魔術', 'PHI': '費城 76 人', 'PHX': '鳳凰城太陽',
    'POR': '波特蘭開拓者', 'SAC': '沙加邁度國王', 'SAS': '聖安東尼奧馬刺',
    'TOR': '多倫多暴龍', 'UTA': '猶他爵士', 'WAS': '華盛頓巫師'
}

st.set_page_config(page_title="NBA 數據專家 v6.9", layout="wide")

# --- [修正] 安全抓取函數：防止 KeyError ---
def safe_api_call(endpoint_func, **kwargs):
    try:
        # 增加一點點延遲防止被 API 封鎖
        time.sleep(0.5)
        return endpoint_func(**kwargs).get_data_frames()[0]
    except Exception as e:
        # 如果該端點出錯，回傳空 DataFrame
        return pd.DataFrame()

# --- 2. 數據抓取 ---
@st.cache_data(ttl=3600)
def load_all_data_v69():
    S = '2025-26'
    ST = 'Regular Season'
    nba_ids = [t['id'] for t in teams.get_teams()]
    
    # 使用安全抓取
    df_base = safe_api_call(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv = safe_api_call(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_hustle = safe_api_call(leaguehustlestatsteam.LeagueHustleStatsTeam, season=S, per_mode_time='PerGame')
    df_spd = safe_api_call(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
    df_pass = safe_api_call(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='Passing', per_mode_simple='PerGame')
    df_trans = safe_api_call(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
    df_iso = safe_api_call(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Isolation', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
    df_rim = safe_api_call(leaguedashptdefend.LeagueDashPtDefend, season=S, defense_category='Less Than 6 Ft')

    def to_map(df, cols):
        if df is None or df.empty: return {}
        id_col = 'TEAM_ID' if 'TEAM_ID' in df.columns else (df.columns[0] if 'ID' in df.columns[0] else None)
        return df.set_index(id_col)[cols].to_dict('index') if id_col else {}

    maps = {
        'base': to_map(df_base, ['PTS', 'REB', 'AST', 'FG_PCT']),
        'adv': to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE']),
        'hustle': to_map(df_hustle, ['DEFLECTIONS', 'CONTESTED_SHOTS']),
        'spd': to_map(df_spd, ['DIST_MILES', 'AVG_SPEED']),
        'pass': to_map(df_pass, ['PASSES_MADE']),
        'trans': to_map(df_trans, ['PPP']),
        'iso': to_map(df_iso, ['PPP']),
        'rim': to_map(df_rim, ['D_FG_PCT'])
    }

    gf_raw = safe_api_call(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    if gf_raw.empty: return None # 嚴重錯誤回傳 None
    
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)

    def gv(tid, m, k, d=0): return maps[m].get(tid, {}).get(k, d)

    feats = ['IS_HOME', 'REST_DAYS', 'F_PTS', 'F_REB', 'F_AST', 'F_ORTG', 'F_DRTG', 'F_PACE', 
             'F_DEFL', 'F_CONT', 'F_DIST', 'F_SPD', 'F_PASS', 'F_TRANS', 'F_ISO', 'F_RIM']
    
    for f, m, k, default in [
        ('F_PTS', 'base', 'PTS', 110), ('F_REB', 'base', 'REB', 44), ('F_AST', 'base', 'AST', 25),
        ('F_ORTG', 'adv', 'OFF_RATING', 110), ('F_DRTG', 'adv', 'DEF_RATING', 110), ('F_PACE', 'adv', 'PACE', 99),
        ('F_DEFL', 'hustle', 'DEFLECTIONS', 15), ('F_CONT', 'hustle', 'CONTESTED_SHOTS', 40),
        ('F_DIST', 'spd', 'DIST_MILES', 18), ('F_SPD', 'spd', 'AVG_SPEED', 4.4),
        ('F_PASS', 'pass', 'PASSES_MADE', 280), ('F_TRANS', 'trans', 'PPP', 1.1),
        ('F_ISO', 'iso', 'PPP', 0.9), ('F_RIM', 'rim', 'D_FG_PCT', 0.6)
    ]:
        gf[f] = gf['TEAM_ID'].apply(lambda x: gv(x, m, k, default))

    clf = xgb.XGBClassifier(n_estimators=100).fit(gf[feats], gf['WIN_BIN'])
    reg = xgb.XGBRegressor(n_estimators=100).fit(gf[feats], gf['PLUS_MINUS'].fillna(0))
    ps_raw = safe_api_call(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    
    return clf, reg, gf, ps_raw, feats, maps, datetime.now(tw_tz).strftime("%H:%M")

# --- 側邊欄控制與鎖定 ---
st.sidebar.title("⚙️ 控制面板")
is_locked = st.sidebar.toggle("🔒 鎖定目前數據", value=False)

if "fixed_data" not in st.session_state or not is_locked:
    with st.spinner("正在同步最新 NBA 數據..."):
        data = load_all_data_v69()
        if data:
            st.session_state.fixed_data = data
        else:
            st.error("無法從 NBA API 獲取數據，請稍後再試。")
            st.stop()

clf, reg, gf, ps_raw, feats, maps, last_update = st.session_state.fixed_data
if is_locked: st.sidebar.warning(f"數據已鎖定於 {last_update}")

st.title(f"🏀 NBA 數據專家 v6.9 (數據鎖定版)")

# --- 3. 介面呈現 ---
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(3)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        sb = safe_api_call(scoreboardv2.ScoreboardV2, game_date=dates[i].strftime('%m/%d/%Y'))
        if sb is None or sb.empty:
            st.info("📅 今日暫無比賽數據")
        else:
            id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
            for _, row in sb.iterrows():
                h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
                h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
                if h_abbr and a_abbr:
                    with st.expander(f"🏟️ {TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}", expanded=True):
                        h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
                        if not h_last.empty:
                            prob = clf.predict_proba(h_last[feats])[0][1] * 100
                            diff = abs(float(reg.predict(h_last[feats])[0]))
                            
                            c1, c2, c3 = st.columns(3)
                            c1.metric(TEAM_NAME_CH.get(h_abbr), f"{prob:.1f}% 勝率")
                            c2.metric(TEAM_NAME_CH.get(a_abbr), f"{100-prob:.1f}% 勝率")
                            c3.metric("預測分差", f"{diff:.1f} 分")

                            def gm(tid, m, k): return maps[m].get(tid, {}).get(k, 0)
                            st.write("---")
                            col_a, col_b = st.columns(2)
                            with col_a:
                                st.table(pd.DataFrame({
                                    "項目": ["得分", "進攻效率", "防守效率", "里程", "速度"],
                                    "數據": [f"{gm(h_id,'base','PTS'):.1f} 分", f"{gm(h_id,'adv','OFF_RATING')} pts", f"{gm(h_id,'adv','DEF_RATING')} pts", f"{gm(h_id,'spd', 'DIST_MILES'):.2f} mi", f"{gm(h_id,'spd','AVG_SPEED'):.2f} mph"]
                                }))
                            with col_b:
                                st.table(pd.DataFrame({
                                    "項目": ["撥球破壞", "場均傳球", "轉換 PPP", "單打 PPP", "護框命中 %"],
                                    "數據": [f"{gm(h_id,'hustle','DEFLECTIONS'):.1f} 次", f"{gm(h_id,'pass','PASSES_MADE'):.1f} 次", f"{gm(h_id,'trans','PPP'):.2f}", f"{gm(h_id,'iso','PPP'):.2f}", f"{gm(h_id,'rim','D_FG_PCT'):.1%}"]
                                }))

st.sidebar.caption(f"🕒 最後更新時間：{last_update}")
