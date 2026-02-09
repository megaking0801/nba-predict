import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    synergyplaytypes
)
from nba_api.stats.static import teams
import pandas as pd
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

st.set_page_config(page_title="NBA 數據專家 v8.2", layout="wide")

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
                    status = cols[2].get_text(strip=True).lower()
                    # 嚴格區分報銷與疑慮
                    is_out = any(k in status for k in ['out', 'season', 'surgery', 'indefinitely', 'broken', 'torn', 'acl', 'mcl'])
                    is_gtd = any(k in status for k in ['questionable', 'doubtful', 'decision', 'probable', 'gtd']) and not is_out
                    
                    team_inj.append({'name': name, 'status': status.upper(), 'is_out': is_out, 'is_gtd': is_gtd})
            injury_data[abbr] = team_inj
        return injury_data
    except: return {}

# --- 3. 數據載入 ---
@st.cache_data(ttl=3600)
def load_all_data_v82():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S = '2025-26'
    
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST', 'MIN']], 
                        ps_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID')
    
    player_db = {normalize_name(row['PLAYER_NAME']): row.to_dict() for _, row in ps_full.iterrows()}
    
    # 團隊與模型數據 (簡略保留)
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    maps = {'adv': df_adv.set_index('TEAM_ID').to_dict('index') if not df_adv.empty else {}}
    
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().apply(lambda x: pd.to_timedelta(x).days).fillna(3)
    
    feats = ['REST_DAYS']
    clf = xgb.XGBClassifier().fit(gf[feats].fillna(0), gf['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(gf[feats].fillna(0), gf['PLUS_MINUS'].fillna(0))
    
    return clf, reg, gf, ps_full, feats, maps, player_db, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, ps_full, feats, maps, player_db, last_update = load_all_data_v82()
injury_report = fetch_live_injuries_espn()

# --- 4. 核心邏輯：區分「帶傷上陣」與「缺陣」 ---
def get_detailed_roster(abbr, team_id):
    team_ps = ps_full[ps_full['TEAM_ID'] == team_id].copy()
    team_ps['norm_name'] = team_ps['PLAYER_NAME'].apply(normalize_name)
    
    inj_list = injury_report.get(abbr, [])
    out_names = [normalize_name(i['name']) for i in inj_list if i['is_out']]
    gtd_names = [normalize_name(i['name']) for i in inj_list if i['is_gtd']]
    
    roster_analysis = []
    total_active_power = 0
    
    for _, p in team_ps.iterrows():
        name_norm = p['norm_name']
        status_label = "✅ 正常"
        weight = 1.0
        
        if name_norm in out_names:
            status_label = "❌ 缺陣"
            weight = 0.0
        elif name_norm in gtd_names:
            status_label = "⚠️ 疑慮"
            weight = 0.5  # 帶傷上陣戰力打折
            
        roster_analysis.append({
            '姓名': p['PLAYER_NAME'],
            '狀態': status_label,
            '場均PTS': p['PTS'],
            '真實命中%': p['TS_PCT'],
            'PIE': p['PIE'],
            '戰力權重': weight
        })
        total_active_power += (p['PTS'] * weight)
        
    return pd.DataFrame(roster_analysis).sort_values('場均PTS', ascending=False), total_active_power

# --- 5. 介面 ---
st.title("🏀 NBA 數據專家 v8.2 (傷病細分版)")

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

        sel_label = st.selectbox("🔍 選擇查看場次 (包含上場/帶傷/缺陣詳細清單)", [g['label'] for g in game_list], key=f"sel_{i}")
        g = next(item for item in game_list if item['label'] == sel_label)
        
        # 獲取細分名單
        h_df, h_power = get_detailed_roster(g['h_abbr'], g['h_id'])
        a_df, a_power = get_detailed_roster(g['a_abbr'], g['a_id'])
        
        # 顯示戰力卡片
        c1, c2 = st.columns(2)
        with c1:
            st.metric(f"🏠 {TEAM_NAME_CH[g['h_abbr']]}", f"可用戰力: {h_power:.1f}")
            st.dataframe(h_df, hide_index=True)
        with c2:
            st.metric(f"✈️ {TEAM_NAME_CH[g['a_abbr']]}", f"可用戰力: {a_power:.1f}")
            st.dataframe(a_df, hide_index=True)

        st.info("💡 戰力權重計算：正常(1.0), 疑慮(0.5), 確定缺陣(0)")

st.sidebar.info(f"🕒 系統更新：{last_update}")
