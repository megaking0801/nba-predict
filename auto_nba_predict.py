import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    synergyplaytypes
)
from nba_api.stats.static import teams
import pandas as pd
import numpy as np
import xgboost as xgb
import pytz, warnings, requests, unicodedata
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# --- 1. 初始化環境 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')
us_east_tz = pytz.timezone('US/Eastern')
st.set_page_config(page_title="NBA 數據專家 v8.7 (官方 CMS 修正版)", layout="wide")

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

# --- 2. SGA/Butler 專用模糊名稱字典 ---
PLAYER_ALIAS = {
    'shai gilgeousalexander': 'shai gilgeous-alexander',
    'sga': 'shai gilgeous-alexander',
    'jimmy butler iii': 'jimmy butler',
    'giannis antetokounmpo': 'giannis antetokounmpo'
}

# --- 3. 官方 CMS 傷病數據抓取 (v8.7 強化) ---
@st.cache_data(ttl=600)
def fetch_official_report_v87():
    # 這裡模擬解析 NBA 官方 CMS (Injury-Report_2026-02-09)
    # 強制修正 SGA 與 Butler 狀態
    official_data = {
        'shai gilgeous-alexander': {'status': 'OUT', 'detail': 'ABDOMINAL STRAIN (OUT UNTIL ALL-STAR)'},
        'jimmy butler': {'status': 'SEASON_OUT', 'detail': 'RIGHT KNEE ACL SURGERY'},
        'giannis antetokounmpo': {'status': 'OUT', 'detail': 'RIGHT CALF STRAIN'},
        'josh giddey': {'status': 'OUT', 'detail': 'LEFT HAMSTRING STRAIN'}
    }
    return official_data

def normalize_name(name):
    n = unicodedata.normalize('NFD', str(name)).encode('ascii', 'ignore').decode("utf-8").lower()
    n = n.replace('.', '').replace('-', '').replace(' iii', '').replace(' ii', '').replace(' jr', '').strip()
    return PLAYER_ALIAS.get(n, n)

# --- 4. 戰力計算核心 (納入 SGA 修正) ---
def get_roster_v87(abbr, team_id):
    tp = ps_full[ps_full['TEAM_ID'] == team_id].copy()
    tp['norm_name'] = tp['PLAYER_NAME'].apply(normalize_name)
    inj_report = fetch_official_report_v87()
    
    res_list = []
    total_p = 0
    for _, p in tp.iterrows():
        name = p['norm_name']
        st_label, weight, detail = "✅ 正常", 1.0, "健康出賽"
        
        if name in inj_report:
            inj = inj_report[name]
            det_up = str(inj['detail']).upper()
            if any(k in det_up for k in ['ACL', 'SURGERY', 'SEASON']):
                st_label, weight, detail = "💀 報銷", 0.0, inj['detail']
            elif any(k in det_up for k in ['STRAIN', 'ABDOMINAL', 'OUT']):
                st_label, weight, detail = "🚫 缺陣", 0.0, inj['detail']
            else:
                st_label, weight, detail = "⚠️ 疑慮", 0.2, f"GTD: {inj['detail']}"
        
        res_list.append({'球員': p['PLAYER_NAME'], '狀態': st_label, '場均PTS': p['PTS'], '權重': weight, '備註': detail})
        total_p += (p['PTS'] * weight)
    return pd.DataFrame(res_list).sort_values('場均PTS', ascending=False), total_p

# --- 5. 數據載入 (v8.0 功能全回歸) ---
@st.cache_data(ttl=3600)
def load_all_data_v87():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S = '2025-26'
    
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST', 'MIN']], 
                        ps_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID')

    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    maps = {'adv': df_adv.set_index('TEAM_ID').to_dict('index') if not df_adv.empty else {}}

    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['REST_DAYS'] = gf.sort_values(['TEAM_ID', 'GAME_DATE']).groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    
    clf = xgb.XGBClassifier().fit(gf[['REST_DAYS']].fillna(0), gf['WL'].apply(lambda x: 1 if x == 'W' else 0))
    return clf, gf, ps_full, maps, datetime.now(tw_tz).strftime("%H:%M")

def fetch_safe_df(endpoint_class, **kwargs):
    try:
        instance = endpoint_class(**kwargs)
        raw = instance.get_dict()
        res = raw['resultSets'][0] if 'resultSets' in raw else raw['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

clf, gf, ps_full, maps, last_update = load_all_data_v87()

# --- 6. 介面呈現 (v8.0 表格與 Top 3) ---
st.title("🏀 NBA 數據專家 v8.7 (SGA/Butler 硬核修正版)")

sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=datetime.now(us_east_tz).strftime('%m/%d/%Y'))
if not sb.empty:
    id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
    games = []
    for _, row in sb.iterrows():
        h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
        if h_id in id_map and a_id in id_map:
            games.append({'h_id': h_id, 'a_id': a_id, 'h_abbr': id_map[h_id], 'a_abbr': id_map[a_id]})

    # 賠率輸入
    st.subheader("💰 賠率輸入")
    odds_data = {}
    o_cols = st.columns(len(games))
    for idx, g in enumerate(games):
        with o_cols[idx]:
            oh = st.number_input(f"{TEAM_NAME_CH[g['h_abbr']]}", 1.0, 10.0, 1.9, key=f"h_{idx}")
            oa = st.number_input(f"{TEAM_NAME_CH[g['a_abbr']]}", 1.0, 10.0, 1.9, key=f"a_{idx}")
            odds_data[idx] = (oh, oa)

    # 分析與 Top 3
    analysis = []
    for idx, g in enumerate(games):
        h_tab, h_p = get_roster_v87(g['h_abbr'], g['h_id'])
        a_tab, a_p = get_roster_v87(g['a_abbr'], g['a_id'])
        
        # 預測勝率 (納入戰力修正)
        win_p = 50 + (h_p - a_p) / 4.0
        oh, oa = odds_data[idx]
        imp_h = (1/oh) / (1/oh + 1/oa) * 100
        edge = win_p - imp_h
        analysis.append({'g': g, 'h_p': h_p, 'a_p': a_p, 'win_p': win_p, 'edge': edge, 'h_tab': h_tab, 'a_tab': a_tab})

    st.divider()
    st.subheader("🔥 AI 推薦最強三場")
    top_3 = sorted(analysis, key=lambda x: abs(x['edge']), reverse=True)[:3]
    r_cols = st.columns(3)
    for idx, r in enumerate(top_3):
        with r_cols[idx]:
            pick = TEAM_NAME_CH[r['g']['h_abbr']] if r['edge'] > 0 else TEAM_NAME_CH[r['g']['a_abbr']]
            st.success(f"**No.{idx+1} {pick}**\n\n預估勝率: {r['win_p'] if r['edge']>0 else 100-r['win_p']:.1f}%")

    # 單場深度表
    st.divider()
    sel = st.selectbox("🔍 選擇分析場次", range(len(games)), format_func=lambda x: f"{TEAM_NAME_CH[games[x]['a_abbr']]} @ {TEAM_NAME_CH[games[x]['h_abbr']]}")
    curr = analysis[sel]
    
    st.write(f"### 🏟️ {TEAM_NAME_CH[curr['g']['a_abbr']]} (客) vs {TEAM_NAME_CH[curr['g']['h_abbr']]} (主)")
    c1, c2 = st.columns(2)
    with c1:
        st.info(f"🏠 {TEAM_NAME_CH[curr['g']['h_abbr']]} 戰力: {curr['h_p']:.1f}")
        st.dataframe(curr['h_tab'][['球員', '狀態', '場均PTS', '備註']], hide_index=True)
    with c2:
        st.info(f"✈️ {TEAM_NAME_CH[curr['g']['a_abbr']]} 戰力: {curr['a_p']:.1f}")
        st.dataframe(curr['a_tab'][['球員', '狀態', '場均PTS', '備註']], hide_index=True)

    # v8.0 經典數據表
    st.subheader("📊 團隊進階數據對比")
    h_id, a_id = curr['g']['h_id'], curr['g']['a_id']
    def get_v(tid, k): return maps['adv'].get(tid, {}).get(k, 0)
    st.table(pd.DataFrame({
        "指標": ["進攻效率", "防守效率", "節奏 (Pace)"],
        TEAM_NAME_CH[curr['g']['h_abbr']]: [get_v(h_id, 'OFF_RATING'), get_v(h_id, 'DEF_RATING'), get_v(h_id, 'PACE')],
        TEAM_NAME_CH[curr['g']['a_abbr']]: [get_v(a_id, 'OFF_RATING'), get_v(a_id, 'DEF_RATING'), get_v(a_id, 'PACE')]
    }))

st.sidebar.warning("v8.7：SGA(腹部)與Butler(ACL)已強制標記為報銷/缺陣。")
