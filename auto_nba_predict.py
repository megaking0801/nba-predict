import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    leaguedashptdefend
)
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import pytz, warnings
from datetime import datetime, timedelta

# --- 1. 基本設定與美式更動紀錄 ---
warnings.filterwarnings('ignore')
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

st.set_page_config(page_title="NBA 數據專家 v6.4", layout="wide")
st.title("🏀 NBA 數據專家 v6.4 (穩定單位補全版)")

# --- 2. 數據抓取邏輯 (v6.4 穩定端點) ---
def fetch_safe_df(endpoint_class, **kwargs):
    try:
        instance = endpoint_class(**kwargs)
        df = instance.get_data_frames()[0]
        id_col = next((c for c in df.columns if 'ID' in c.upper()), None)
        if id_col: df['TEAM_ID'] = df[id_col].astype(int)
        return df
    except: return pd.DataFrame()

@st.cache_data(ttl=3600)
def load_data_v64():
    S = '2025-26'
    ST = 'Regular Season'
    nba_ids = [t['id'] for t in teams.get_teams()]
    
    # [抓取維度]
    df_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_hustle = fetch_safe_df(leaguehustlestatsteam.LeagueHustleStatsTeam, season=S, per_mode_time='PerGame')
    df_spd = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
    df_pass = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='Passing', per_mode_simple='PerGame')
    df_rim = fetch_safe_df(leaguedashptdefend.LeagueDashPtDefend, season=S, defense_category='Less Than 6 Ft', season_type_all_star=ST)

    def to_map(df, cols):
        return df.set_index('TEAM_ID')[cols].to_dict('index') if not df.empty and 'TEAM_ID' in df.columns else {}

    maps = {
        'base': to_map(df_base, ['PTS', 'REB', 'AST']),
        'adv': to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE']),
        'hustle': to_map(df_hustle, ['DEFLECTIONS']),
        'spd': to_map(df_spd, ['DIST_MILES', 'AVG_SPEED']),
        'pass': to_map(df_pass, ['PASSES_MADE']),
        'rim': to_map(df_rim, ['D_FG_PCT'])
    }

    # [模型訓練]
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'], errors='coerce')
    gf = gf.dropna(subset=['GAME_DATE']).sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0).astype(int)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0).astype(int)
    
    def gv(tid, m, k, d=0): return maps[m].get(int(tid), {}).get(k, d)
    
    # v6.4 特徵組
    feats = ['IS_HOME', 'F_PTS', 'F_ORTG', 'F_DRTG', 'F_PACE', 'F_DIST', 'F_SPD', 'F_PASS']
    gf['F_PTS'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'base', 'PTS', 110))
    gf['F_ORTG'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'adv', 'OFF_RATING', 110))
    gf['F_DRTG'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'adv', 'DEF_RATING', 110))
    gf['F_PACE'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'adv', 'PACE', 99))
    gf['F_DIST'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'spd', 'DIST_MILES', 18))
    gf['F_SPD'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'spd', 'AVG_SPEED', 4.4))
    gf['F_PASS'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'pass', 'PASSES_MADE', 280))

    train_x = gf[feats].astype(float).fillna(0)
    clf = xgb.XGBClassifier(n_estimators=100).fit(train_x, gf['WIN_BIN'])
    
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    return clf, gf, ps_raw, feats, maps, datetime.now(tw_tz).strftime("%H:%M")

clf, gf, ps_raw, feats, maps, last_update = load_data_v64()

# --- 3. 介面呈現 ---
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(3)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates[i].strftime('%m/%d/%Y'))
        if sb.empty: st.info("📅 無賽程數據")
        else:
            id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
            for _, row in sb.iterrows():
                h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
                h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
                
                if h_abbr and a_abbr:
                    with st.expander(f"🏀 {TEAM_NAME_CH.get(a_abbr)} vs {TEAM_NAME_CH.get(h_abbr)}", expanded=True):
                        h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
                        if not h_last.empty:
                            test_x = h_last[feats].astype(float).fillna(0)
                            prob = clf.predict_proba(test_x)[0][1] * 100
                            
                            c1, c2 = st.columns(2)
                            c1.metric(TEAM_NAME_CH.get(h_abbr), f"{prob:.1f}% 勝率")
                            c2.metric(TEAM_NAME_CH.get(a_abbr), f"{100-prob:.1f}% 勝率")
                            
                            def gm(m, tid, k): return maps[m].get(int(tid), {}).get(k, 0)
                            
                            # 數據表格 (補上單位)
                            st.table(pd.DataFrame({
                                "數據項目": ["場均得分", "進攻效率", "防守效率", "跑動里程", "平均速度", "場均傳球", "護框命中率"],
                                TEAM_NAME_CH.get(h_abbr): [f"{gm('base',h_id,'PTS'):.1f} 分", f"{gm('adv',h_id,'OFF_RATING')} pts", f"{gm('adv',h_id,'DEF_RATING')} pts", f"{gm('spd',h_id,'DIST_MILES'):.2f} mi", f"{gm('spd',h_id,'AVG_SPEED'):.2f} mph", f"{gm('pass',h_id,'PASSES_MADE'):.1f} 次", f"{gm('rim',h_id,'D_FG_PCT'):.1%}"],
                                TEAM_NAME_CH.get(a_abbr): [f"{gm('base',a_id,'PTS'):.1f} 分", f"{gm('adv',a_id,'OFF_RATING')} pts", f"{gm('adv',a_id,'DEF_RATING')} pts", f"{gm('spd',a_id,'DIST_MILES'):.2f} mi", f"{gm('spd',a_id,'AVG_SPEED'):.2f} mph", f"{gm('pass',a_id,'PASSES_MADE'):.1f} 次", f"{gm('rim',a_id,'D_FG_PCT'):.1%}"]
                            }))

st.sidebar.caption(f"🕒 更新：{last_update}")
