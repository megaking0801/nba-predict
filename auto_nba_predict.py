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

# --- 1. 基本設定 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')
us_east_tz = pytz.timezone('US/Eastern')

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

TEAM_NAME_EN_MAP = {
    'Atlanta': 'ATL', 'Brooklyn': 'BKN', 'Boston': 'BOS', 'Charlotte': 'CHA',
    'Chicago': 'CHI', 'Cleveland': 'CLE', 'Dallas': 'DAL', 'Denver': 'DEN',
    'Detroit': 'DET', 'Golden State': 'GSW', 'Houston': 'HOU', 'Indiana': 'IND',
    'LA Clippers': 'LAC', 'LA Lakers': 'LAL', 'Memphis': 'MEM', 'Miami': 'MIA',
    'Milwaukee': 'MIL', 'Minnesota': 'MIN', 'New Orleans': 'NOP', 'New York': 'NYK',
    'Oklahoma City': 'OKC', 'Orlando': 'ORL', 'Philadelphia': 'PHI', 'Phoenix': 'PHX',
    'Portland': 'POR', 'Sacramento': 'SAC', 'San Antonio': 'SAS', 'Toronto': 'TOR',
    'Utah': 'UTA', 'Washington': 'WAS'
}

st.set_page_config(page_title="NBA 數據專家 v8.3", layout="wide")

# --- 2. 工具函數 ---
def normalize_name(name):
    if not isinstance(name, str): return ""
    return unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode("utf-8").lower().replace('.', '').strip()

def fetch_safe_df(endpoint_class, **kwargs):
    try:
        instance = endpoint_class(**kwargs)
        raw = instance.get_dict()
        res = raw['resultSets'][0] if 'resultSets' in raw else raw['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

@st.cache_data(ttl=600)
def fetch_live_injuries_espn():
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        injury_data = {}
        sections = soup.find_all(class_='Table__Title')
        for section in sections:
            team_raw = section.get_text().strip()
            abbr = next((a for n, a in TEAM_NAME_EN_MAP.items() if n in team_raw), None)
            if not abbr: continue
            table = section.find_next('table')
            rows = table.find_all('tr')[1:]
            team_inj = []
            for row in rows:
                cols = row.find_all('td')
                if len(cols) >= 3:
                    name = cols[0].get_text(strip=True)
                    status_text = cols[2].get_text(strip=True).lower()
                    
                    # 超細緻分類
                    is_season_out = any(k in status_text for k in ['season', 'surgery', 'torn', 'broken', 'acl', 'mcl', 'achilles', 'fracture'])
                    is_out = 'out' in status_text and not is_season_out
                    is_gtd = any(k in status_text for k in ['questionable', 'doubtful', 'decision', 'probable', 'gtd'])
                    
                    final_status = "SEASON_OUT" if is_season_out else ("OUT" if is_out else "GTD")
                    team_inj.append({'name': name, 'status': final_status, 'raw_text': status_text.upper()})
            injury_data[abbr] = team_inj
        return injury_data
    except: return {}

# --- 3. 數據核心 (修正 REST_DAYS Bug) ---
@st.cache_data(ttl=3600)
def load_all_data_v83():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S, ST = '2025-26', 'Regular Season'
    
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST', 'MIN']], 
                        ps_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID')
    
    # 團隊指標
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    maps = {'adv': df_adv.set_index('TEAM_ID').to_dict('index') if not df_adv.empty else {}}
    
    # 修正後的 REST_DAYS 計算
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    
    # 使用 diff 並轉換為天數數值，避免 TypeError
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    
    feats = ['REST_DAYS']
    clf = xgb.XGBClassifier().fit(gf[feats].fillna(0), gf['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(gf[feats].fillna(0), gf['PLUS_MINUS'].fillna(0))
    
    player_db = {normalize_name(row['PLAYER_NAME']): row.to_dict() for _, row in ps_full.iterrows()}
    
    return clf, reg, gf, ps_full, feats, maps, player_db, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, ps_full, feats, maps, player_db, last_update = load_all_data_v83()
injury_report = fetch_live_injuries_espn()

# --- 4. 核心邏輯：深度名單分析 ---
def get_detailed_roster_v83(abbr, team_id):
    team_players = ps_full[ps_full['TEAM_ID'] == team_id].copy()
    team_players['norm_name'] = team_players['PLAYER_NAME'].apply(normalize_name)
    
    inj_data = injury_report.get(abbr, [])
    inj_map = {normalize_name(i['name']): i for i in inj_data}
    
    roster_list = []
    total_power = 0
    
    for _, p in team_players.iterrows():
        name_norm = p['norm_name']
        status_label = "✅ 正常"
        weight = 1.0
        detail = "健康"
        
        if name_norm in inj_map:
            inj = inj_map[name_norm]
            if inj['status'] == "SEASON_OUT":
                status_label = "💀 報銷"
                weight = 0.0
                detail = f"嚴重傷病: {inj['raw_text']}"
            elif inj['status'] == "OUT":
                status_label = "🚫 缺陣"
                weight = 0.0
                detail = "不確定回歸日期"
            elif inj['status'] == "GTD":
                status_label = "⚠️ 疑慮"
                weight = 0.4 # 帶傷上陣，戰力保守估計
                detail = f"賽前決定: {inj['raw_text']}"
        
        roster_list.append({
            '球員': p['PLAYER_NAME'],
            '狀態': status_label,
            '場均PTS': p['PTS'],
            'TS%': p['TS_PCT'],
            '戰力權重': weight,
            '備註': detail
        })
        total_power += (p['PTS'] * weight)
        
    df_res = pd.DataFrame(roster_list).sort_values('場均PTS', ascending=False)
    return df_res, total_power

# --- 5. 介面設計 ---
st.title("🏀 NBA 數據專家 v8.3 (精準修正版)")

# 日期選擇
nba_now = datetime.now(us_east_tz)
dates_nba = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates_nba])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates_nba[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 目前無比賽資訊")
            continue

        id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        game_list = []
        for _, row in sb.iterrows():
            h_abbr, a_abbr = id_to_abbr.get(row['HOME_TEAM_ID']), id_to_abbr.get(row['VISITOR_TEAM_ID'])
            if h_abbr and a_abbr:
                game_list.append({'label': f"{TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}", 'h_id': row['HOME_TEAM_ID'], 'a_id': row['VISITOR_TEAM_ID'], 'h_abbr': h_abbr, 'a_abbr': a_abbr})

        sel_label = st.selectbox("🔍 選擇分析場次", [g['label'] for g in game_list], key=f"sel_{i}")
        g = next(item for item in game_list if item['label'] == sel_label)
        
        # 深度分析
        h_roster, h_power = get_detailed_roster_v83(g['h_abbr'], g['h_id'])
        a_roster, a_power = get_detailed_roster_v83(g['a_abbr'], g['a_id'])
        
        # 戰力對比卡片
        col1, col2 = st.columns(2)
        with col1:
            st.subheader(f"🏠 {TEAM_NAME_CH[g['h_abbr']]}")
            st.metric("預估可用火力 (PPG)", f"{h_power:.1f}")
            st.dataframe(h_roster, hide_index=True, use_container_width=True)
            
        with col2:
            st.subheader(f"✈️ {TEAM_NAME_CH[g['a_abbr']]}")
            st.metric("預估可用火力 (PPG)", f"{a_power:.1f}")
            st.dataframe(a_roster, hide_index=True, use_container_width=True)

        st.divider()
        st.write("📌 **狀態說明**：`💀 報銷` 與 `🚫 缺陣` 不計入戰力；`⚠️ 疑慮` 計入 40% 戰力；其餘為正常。")

# 側邊欄資訊
st.sidebar.info(f"🕒 數據最後更新：{last_update}")
st.sidebar.markdown("""
### v8.3 修正與強化
- **Bug Fix**: 修復了計算休息天數時的類型錯誤。
- **深度傷病分類**: 
  - 自動識別 `Season Out` (ACL/手術/骨折)。
  - 區分 `GTD` 與 `Out`。
- **全體球員分析**: 不論健康與否，所有球員數據皆會列出並參與戰力權重計算。
""")
