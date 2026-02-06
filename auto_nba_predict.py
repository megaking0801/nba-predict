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

# 安全抓取函數
def safe_api_call(endpoint_func, **kwargs):
    try:
        time.sleep(0.6) # 稍微加長延遲確保穩定
        return endpoint_func(**kwargs).get_data_frames()[0]
    except:
        return pd.DataFrame()

# --- 2. 數據抓取核心 ---
@st.cache_data(ttl=3600)
def load_all_data_v69():
    S, ST = '2025-26', 'Regular Season'
    nba_ids = [t['id'] for t in teams.get_teams()]
    
    df_base = safe_api_call(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv = safe_api_call(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_hustle = safe_api_call(leaguehustlestatsteam.LeagueHustleStatsTeam, season=S, per_mode_time='PerGame')
    df_spd = safe_api_call(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
    df_pass = safe_api_call(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='Passing', per_mode_simple='PerGame')
    df_trans = safe_api_call(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
    df_iso = safe_api_call(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Isolation', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
    df_rim = safe_api_call(leaguedashptdefend.LeagueDashPtDefend, season=S, defense_category='Less Than 6 Ft')

    def to_map(df, cols):
        if df.empty: return {}
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
    if gf_raw.empty: return None
    
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
    
    return clf, reg, gf, feats, maps, datetime.now(tw_tz).strftime("%H:%M")

# --- 3. 鎖定控制邏輯 ---
st.sidebar.title("⚙️ 控制面板")
is_locked = st.sidebar.toggle("🔒 鎖定目前數據", value=False)

if "fixed_data" not in st.session_state or not is_locked:
    with st.spinner("同步數據中..."):
        res = load_all_data_v69()
        if res: st.session_state.fixed_data = res
        else: st.stop()

clf, reg, gf, feats, maps, last_update = st.session_state.fixed_data
st.title(f"🏀 NBA 數據專家 v6.9")
if is_locked: st.sidebar.warning(f"數據已鎖定於 {last_update}")

# --- 4. 介面呈現 (下拉選單 UI) ---
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(3)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        current_date = dates[i].strftime('%m/%d/%Y')
        sb = safe_api_call(scoreboardv2.ScoreboardV2, game_date=current_date)
        
        if sb.empty:
            st.info("📅 今日暫無比賽")
        else:
            id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
            game_list = []
            game_data = {}
            
            for _, row in sb.iterrows():
                h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
                h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
                if h_abbr and a_abbr:
                    label = f"{TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}"
                    game_list.append(label)
                    game_data[label] = (h_id, a_id, h_abbr, a_abbr)
            
            if game_list:
                selected_game = st.selectbox("🎯 選擇場次進行 AI 分析", game_list, key=f"sb_{i}")
                h_id, a_id, h_abbr, a_abbr = game_data[selected_game]
                
                h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
                if not h_last.empty:
                    prob = clf.predict_proba(h_last[feats])[0][1] * 100
                    diff = abs(float(reg.predict(h_last[feats])[0]))
                    
                    # 預測大標題
                    st.subheader(f"🏟️ 分析結果：{selected_game}")
                    c1, c2, c3 = st.columns(3)
                    c1.metric(TEAM_NAME_CH.get(h_abbr), f"{prob:.1f}% 勝率")
                    c2.metric(TEAM_NAME_CH.get(a_abbr), f"{100-prob:.1f}% 勝率")
                    c3.metric("AI 預測分差", f"{diff:.1f} 分")

                    def gm(tid, m, k): return maps[m].get(tid, {}).get(k, 0)
                    
                    st.write("---")
                    # v6.9 標準雙欄表格 UI
                    col_left, col_right = st.columns(2)
                    with col_left:
                        st.write("**📊 團隊戰力指標 (主隊)**")
                        st.table(pd.DataFrame({
                            "項目": ["得分", "進攻效率", "防守效率", "里程", "速度"],
                            "數據": [f"{gm(h_id,'base','PTS'):.1f} 分", f"{gm(h_id,'adv','OFF_RATING')} pts", f"{gm(h_id,'adv','DEF_RATING')} pts", f"{gm(h_id,'spd','DIST_MILES'):.2f} mi", f"{gm(h_id,'spd','AVG_SPEED'):.2f} mph"]
                        }))
                    with col_right:
                        st.write("**⚔️ 戰術與積極度 (主隊)**")
                        st.table(pd.DataFrame({
                            "項目": ["撥球破壞", "場均傳球", "轉換 PPP", "單打 PPP", "護框命中 %"],
                            "數據": [f"{gm(h_id,'hustle','DEFLECTIONS'):.1f} 次", f"{gm(h_id,'pass','PASSES_MADE'):.1f} 次", f"{gm(h_id,'trans','PPP'):.2f}", f"{gm(h_id,'iso','PPP'):.2f}", f"{gm(h_id,'rim','D_FG_PCT'):.1%}"]
                        }))

st.sidebar.caption(f"🕒 更新：{last_update}")
