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
st.title("🏀 NBA 數據專家 v6.9 (16項指標聯動版)")

# --- 2. 數據抓取 (回歸原始純化邏輯) ---
@st.cache_data(ttl=3600)
def load_all_data_v69():
    S = '2025-26'
    ST = 'Regular Season'
    nba_ids = [t['id'] for t in teams.get_teams()]
    
    # 抓取各維度數據
    df_base = leaguedashteamstats.LeagueDashTeamStats(season=S, per_mode_detailed='PerGame').get_data_frames()[0]
    df_adv = leaguedashteamstats.LeagueDashTeamStats(season=S, measure_type_detailed_defense='Advanced').get_data_frames()[0]
    df_hustle = leaguehustlestatsteam.LeagueHustleStatsTeam(season=S, per_mode_time='PerGame').get_data_frames()[0]
    df_spd = leaguedashptstats.LeagueDashPtStats(season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame').get_data_frames()[0]
    df_pass = leaguedashptstats.LeagueDashPtStats(season=S, pt_measure_type='Passing', per_mode_simple='PerGame').get_data_frames()[0]
    df_trans = synergyplaytypes.SynergyPlayTypes(play_type_nullable='Transition', player_or_team_abbreviation='T', season=S, season_type_all_star=ST).get_data_frames()[0]
    df_iso = synergyplaytypes.SynergyPlayTypes(play_type_nullable='Isolation', player_or_team_abbreviation='T', season=S, season_type_all_star=ST).get_data_frames()[0]
    df_rim = leaguedashptdefend.LeagueDashPtDefend(season=S, defense_category='Less Than 6 Ft').get_data_frames()[0]

    def to_map(df, cols):
        if df.empty: return {}
        # 自動識別 ID 欄位
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

    # 模型訓練
    gf = leaguegamefinder.LeagueGameFinder(season_nullable=S).get_data_frames()[0]
    gf = gf[gf['TEAM_ID'].isin(nba_ids)]
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    
    # 計算休息天數
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)

    def gv(tid, m, k, d=0):
        return maps[m].get(tid, {}).get(k, d)

    # 16 項模型指標 (對應視覺化 16 欄位)
    feats = ['IS_HOME', 'REST_DAYS', 'F_PTS', 'F_REB', 'F_AST', 'F_ORTG', 'F_DRTG', 'F_PACE', 
             'F_DEFL', 'F_CONT', 'F_DIST', 'F_SPD', 'F_PASS', 'F_TRANS', 'F_ISO', 'F_RIM']
    
    gf['F_PTS'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'base', 'PTS', 110))
    gf['F_REB'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'base', 'REB', 44))
    gf['F_AST'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'base', 'AST', 25))
    gf['F_ORTG'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'adv', 'OFF_RATING', 110))
    gf['F_DRTG'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'adv', 'DEF_RATING', 110))
    gf['F_PACE'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'adv', 'PACE', 99))
    gf['F_DEFL'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'hustle', 'DEFLECTIONS', 15))
    gf['F_CONT'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'hustle', 'CONTESTED_SHOTS', 40))
    gf['F_DIST'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'spd', 'DIST_MILES', 18))
    gf['F_SPD'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'spd', 'AVG_SPEED', 4.4))
    gf['F_PASS'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'pass', 'PASSES_MADE', 280))
    gf['F_TRANS'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'trans', 'PPP', 1.1))
    gf['F_ISO'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'iso', 'PPP', 0.9))
    gf['F_RIM'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'rim', 'D_FG_PCT', 0.6))

    # 訓練雙模型
    clf = xgb.XGBClassifier(n_estimators=100).fit(gf[feats], gf['WIN_BIN'])
    reg = xgb.XGBRegressor(n_estimators=100).fit(gf[feats], gf['PLUS_MINUS'].fillna(0))
    
    ps_raw = leaguedashplayerstats.LeagueDashPlayerStats(season=S, per_mode_detailed='PerGame').get_data_frames()[0]
    
    return clf, reg, gf, ps_raw, feats, maps, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, ps_raw, feats, maps, last_update = load_all_data_v69()

# --- 3. 介面設計 ---
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(3)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        sb = scoreboardv2.ScoreboardV2(game_date=dates[i].strftime('%m/%d/%Y')).get_data_frames()[0]
        if sb.empty: st.info("📅 目前無賽程")
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
                            c1.metric(TEAM_NAME_CH.get(h_abbr), f"{prob:.1f}%")
                            c2.metric(TEAM_NAME_CH.get(a_abbr), f"{100-prob:.1f}%")
                            c3.metric("預測分差", f"{diff:.1f} 分")

                            def gm(m, tid, k): return maps[m].get(tid, {}).get(k, 0)

                            # 16 項指標聯動表格
                            st.write("---")
                            col_a, col_b = st.columns(2)
                            with col_a:
                                st.write("**📊 團隊戰力指標 (含單位)**")
                                st.table(pd.DataFrame({
                                    "項目": ["得分", "進攻效率", "防守效率", "跑動里程", "平均速度"],
                                    "數據": [f"{gm('base',h_id,'PTS'):.1f} 分", f"{gm('adv',h_id,'OFF_RATING')} pts", f"{gm('adv',h_id,'DEF_RATING')} pts", f"{gm('spd',h_id,'DIST_MILES'):.2f} mi", f"{gm('spd',h_id,'AVG_SPEED'):.2f} mph"]
                                }))
                            with col_b:
                                st.write("**⚔️ 戰術與積極度**")
                                st.table(pd.DataFrame({
                                    "項目": ["撥球破壞", "場均傳球", "轉換 PPP", "單打 PPP", "護框命中 %"],
                                    "數據": [f"{gm('hustle',h_id,'DEFLECTIONS'):.1f} 次", f"{gm('pass',h_id,'PASSES_MADE'):.1f} 次", f"{gm('trans',h_id,'PPP'):.2f}", f"{gm('iso',h_id,'PPP'):.2f}", f"{gm('rim',h_id,'D_FG_PCT'):.1%}"]
                                }))

st.sidebar.caption(f"🕒 最新同步：{last_update}")
