import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    synergyplaytypes, leaguedashptdefend
)
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import pytz, warnings, requests, unicodedata
import numpy as np
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# --- 1. 基本設定 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')
us_east_tz = pytz.timezone('US/Eastern')

# 英文隊名對照 (CBS 爬蟲用)
TEAM_NAME_EN_MAP = {
    'Atlanta Hawks': 'ATL', 'Brooklyn Nets': 'BKN', 'Boston Celtics': 'BOS',
    'Charlotte Hornets': 'CHA', 'Chicago Bulls': 'CHI', 'Cleveland Cavaliers': 'CLE',
    'Dallas Mavericks': 'DAL', 'Denver Nuggets': 'DEN', 'Detroit Pistons': 'DET',
    'Golden State Warriors': 'GSW', 'Houston Rockets': 'HOU', 'Indiana Pacers': 'IND',
    'Los Angeles Clippers': 'LAC', 'L.A. Clippers': 'LAC', 'Los Angeles Lakers': 'LAL', 'L.A. Lakers': 'LAL',
    'Memphis Grizzlies': 'MEM', 'Miami Heat': 'MIA', 'Milwaukee Bucks': 'MIL',
    'Minnesota Timberwolves': 'MIN', 'New Orleans Pelicans': 'NOP', 'New York Knicks': 'NYK',
    'Oklahoma City Thunder': 'OKC', 'Orlando Magic': 'ORL', 'Philadelphia 76ers': 'PHI',
    'Phoenix Suns': 'PHX', 'Portland Trail Blazers': 'POR', 'Sacramento Kings': 'SAC',
    'San Antonio Spurs': 'SAS', 'Toronto Raptors': 'TOR', 'Utah Jazz': 'UTA', 'Washington Wizards': 'WAS'
}

# 中文隊名顯示
TEAM_NAME_CH = {v: k for k, v in {
    '亞特蘭大老鷹': 'ATL', '布魯克林籃網': 'BKN', '波士頓塞爾提克': 'BOS',
    '夏洛特黃蜂': 'CHA', '芝加哥公牛': 'CHI', '克里夫蘭騎士': 'CLE',
    '達拉斯獨行俠': 'DAL', '丹佛金塊': 'DEN', '底特律活塞': 'DET',
    '金州勇士': 'GSW', '休士頓火箭': 'HOU', '印第安納溜馬': 'IND',
    '洛杉磯快艇': 'LAC', '洛杉磯湖人': 'LAL', '曼非斯灰熊': 'MEM',
    '邁阿密熱火': 'MIA', '密爾瓦基公鹿': 'MIL', '明尼蘇達灰狼': 'MIN',
    '紐奧良鵜鶘': 'NOP', '紐約尼克': 'NYK', '奧克拉荷馬雷霆': 'OKC',
    '奧蘭多魔術': 'ORL', '費城 76 人': 'PHI', '鳳凰城太陽': 'PHX',
    '波特蘭開拓者': 'POR', '沙加邁度國王': 'SAC', '聖安東尼奧馬刺': 'SAS',
    '多倫多暴龍': 'TOR', '猶他爵士': 'UTA', '華盛頓巫師': 'WAS'
}.items()}

st.set_page_config(page_title="NBA 數據專家 v7.2", layout="wide")
st.title("🏀 NBA 數據專家 v7.2 (傷病權重自動修正版)")

# --- 2. 核心功能函數 ---

def normalize_name(name):
    """標準化球員姓名以進行比對 (移除特殊符號、轉小寫)"""
    if not isinstance(name, str): return ""
    name = unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode("utf-8")
    return name.lower().replace('.', '').strip()

def fetch_safe_df(endpoint_class, **kwargs):
    try:
        instance = endpoint_class(**kwargs)
        raw = instance.get_dict()
        res = raw['resultSets'][0] if 'resultSets' in raw else raw['resultSet']
        df = pd.DataFrame(res['rowSet'], columns=res['headers'])
        if 'TEAM_ID' in df.columns: df['TEAM_ID'] = df['TEAM_ID'].astype(int)
        return df
    except: return pd.DataFrame()

# 🔥 爬取傷病並解析狀態
@st.cache_data(ttl=600)
def fetch_live_injuries():
    url = "https://www.cbssports.com/nba/injuries/"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        injury_data = {}
        
        for section in soup.find_all('div', class_='TeamLogoNameLockup-name'):
            team_raw = section.get_text().strip()
            abbr = TEAM_NAME_EN_MAP.get(team_raw)
            if not abbr: continue
            
            table = section.find_next('table')
            rows = table.find_all('tr')[1:]
            team_injuries = []
            
            for row in rows:
                cols = row.find_all('td')
                if len(cols) >= 3:
                    name = cols[0].get_text(strip=True)
                    status = cols[2].get_text(strip=True) # Out, Questionable, Day-to-Day
                    desc = cols[3].get_text(strip=True) if len(cols) > 3 else ""
                    
                    # 判斷是否出戰
                    is_out = any(x in status.lower() for x in ['out', 'injured', 'susp'])
                    is_dqs = any(x in status.lower() for x in ['day-to-day', 'questionable', 'doubtful'])
                    
                    team_injuries.append({
                        'name': name,
                        'status': status,
                        'desc': desc,
                        'is_out': is_out,
                        'is_dqs': is_dqs
                    })
            injury_data[abbr] = team_injuries
        return injury_data
    except: return {}

@st.cache_data(ttl=3600)
def load_data_and_model():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S = '2025-26'
    
    # 訓練模型數據
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    
    gf['ROLL_PTS'] = gf.groupby('TEAM_ID')['PTS'].transform(lambda x: x.rolling(5).mean())
    gf['ROLL_DIFF'] = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.rolling(5).mean())
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    
    feats = ['ROLL_PTS', 'ROLL_DIFF']
    train_df = gf.dropna(subset=feats)
    clf = xgb.XGBClassifier(n_estimators=100, learning_rate=0.05).fit(train_df[feats], train_df['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(train_df[feats], train_df['PLUS_MINUS'])
    
    # 球員數據 (用於計算傷病權重)
    ps = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    # 建立姓名查找字典 (Normalize Key -> PPG)
    player_stats = {}
    if not ps.empty:
        for _, row in ps.iterrows():
            norm_name = normalize_name(row['PLAYER_NAME'])
            player_stats[norm_name] = row['PTS']
            
    return clf, reg, gf, feats, player_stats, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, feats, player_stats, last_update = load_data_and_model()
injury_report = fetch_live_injuries()

# --- 3. 邏輯運算：計算傷病衝擊值 ---
def calculate_injury_impact(team_abbr, injuries, player_stats_db):
    """計算球隊因傷病損失的勝率百分比"""
    impact_score = 0
    details = []
    
    for inj in injuries:
        p_name = normalize_name(inj['name'])
        ppg = player_stats_db.get(p_name, 0) # 查不到視為 0 分
        
        weight = 0
        if inj['is_out']: weight = 1.0
        elif inj['is_dqs']: weight = 0.5 # 每日觀察算一半權重
        
        # 權重規則 (Star Value System)
        penalty = 0
        if ppg >= 25: penalty = 12.0 # 超級巨星 (-12%)
        elif ppg >= 18: penalty = 7.0 # 核心 (-7%)
        elif ppg >= 12: penalty = 3.0 # 主力 (-3%)
        elif ppg >= 8: penalty = 1.0  # 輪替 (-1%)
        
        final_penalty = penalty * weight
        if final_penalty > 0:
            impact_score += final_penalty
            status_icon = "❌" if inj['is_out'] else "⚠️"
            details.append(f"{status_icon} {inj['name']} ({ppg:.1f}分) -{final_penalty:.1f}%")
            
    return impact_score, details

# --- 4. 介面顯示 ---
nba_now = datetime.now(us_east_tz)
dates_nba = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates_nba])

for i, tab in enumerate(tabs):
    with tab:
        search_date = dates_nba[i].strftime('%m/%d/%Y')
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=search_date)
        
        if sb.empty:
            st.info(f"📅 {dates_nba[i].strftime('%Y-%m-%d')} 暫無賽程")
        else:
            id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
            for _, row in sb.iterrows():
                h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
                h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
                
                if h_abbr and a_abbr:
                    # 1. 基礎模型預測
                    h_recent = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
                    if h_recent.empty: continue
                    
                    base_prob = clf.predict_proba(h_recent[feats])[0][1] * 100
                    
                    # 2. 計算傷病扣分
                    h_inj = injury_report.get(h_abbr, [])
                    a_inj = injury_report.get(a_abbr, [])
                    
                    h_impact, h_details = calculate_injury_impact(h_abbr, h_inj, player_stats)
                    a_impact, a_details = calculate_injury_impact(a_abbr, a_inj, player_stats)
                    
                    # 3. 修正勝率 (主隊勝率 = 基礎 - 主隊傷病 + 客隊傷病)
                    final_prob = base_prob - h_impact + a_impact
                    final_prob = max(5, min(95, final_prob)) # 限制在 5%-95% 之間
                    
                    # 顯示卡片
                    with st.container():
                        st.markdown(f"#### 🏟️ {TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}")
                        c1, c2, c3 = st.columns(3)
                        
                        # 顯示顏色：如果勝率修正幅度很大，顯示警示
                        delta = final_prob - base_prob
                        
                        c1.metric(f"🏠 {TEAM_NAME_CH.get(h_abbr)}", f"{final_prob:.1f}%", f"修正: {(-h_impact):.1f}%")
                        c2.metric(f"✈️ {TEAM_NAME_CH.get(a_abbr)}", f"{100-final_prob:.1f}%", f"修正: {(-a_impact):.1f}%")
                        
                        winner = TEAM_NAME_CH.get(h_abbr) if final_prob > 50 else TEAM_NAME_CH.get(a_abbr)
                        c3.metric("AI 最終預測", winner, f"原始勝率: {base_prob:.1f}%")
                        
                        # --- 傷病名單顯示區 ---
                        if h_details or a_details:
                            with st.expander("🚑 傷病修正細節 (已計入勝率)", expanded=True):
                                ec1, ec2 = st.columns(2)
                                with ec1:
                                    st.caption(f"**{TEAM_NAME_CH.get(h_abbr)} 缺陣影響**")
                                    if h_details:
                                        for d in h_details: st.error(d)
                                    else: st.success("全員健康")
                                with ec2:
                                    st.caption(f"**{TEAM_NAME_CH.get(a_abbr)} 缺陣影響**")
                                    if a_details:
                                        for d in a_details: st.error(d)
                                    else: st.success("全員健康")
                        st.divider()

st.sidebar.caption(f"🕒 更新時間：{last_update}")
st.sidebar.info("ℹ️ 傷病權重規則：\n巨星(>25分): -12%\n核心(>18分): -7%\n主力(>12分): -3%")
