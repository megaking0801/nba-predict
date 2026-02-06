import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv3, leaguedashplayerstats
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import pytz, warnings, json
from datetime import datetime
from google import genai

# --- 1. AI & 基本設定 (持續記住每次更動) ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')

@st.cache_resource
def get_ai_client():
    if "GEMINI_API_KEY" in st.secrets:
        try:
            return genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
        except: return None
    return None

client = get_ai_client()

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

st.set_page_config(page_title="NBA AI v7.7", layout="wide")
st.title("🏀 NBA 數據預測專家 v7.7")

# --- 2. 獲取基礎模型數據 (加入球隊過濾) ---
@st.cache_data(ttl=3600)
def prepare_model():
    # 1. 取得 30 支球隊的正式 ID 列表
    nba_teams = teams.get_teams()
    nba_ids = [t['id'] for t in nba_teams]
    
    # 2. 抓取數據並過濾掉非 NBA 球隊 (如 G-League)
    gf_raw = leaguegamefinder.LeagueGameFinder(season_nullable='2025-26').get_data_frames()[0]
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['L10_WIN_RATE'] = gf.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    gf['L5_PTS'] = gf.groupby('TEAM_ID')['PTS'].transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    gf['L5_PLUS_MINUS'] = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    gf['B2B'] = (gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days == 1).astype(int)
    
    feats = ['L5_PTS', 'L5_PLUS_MINUS', 'B2B', 'IS_HOME', 'L10_WIN_RATE']
    train = gf.fillna(0)
    
    clf = xgb.XGBClassifier().fit(train[feats], train['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(train[feats], train['PLUS_MINUS'])
    
    return clf, reg, gf, feats

clf, reg, gf, feats = prepare_model()

# --- 3. 抓取今日賽程 ---
# 2026-02-06
today_str = datetime.now(tw_tz).strftime('%Y-%m-%d')
try:
    sb_raw = scoreboardv3.ScoreboardV3(game_date=today_str).get_data_frames()[0]
except:
    sb_raw = pd.DataFrame()

if sb_raw.empty:
    st.info(f"📅 {today_str} 目前尚未發布比賽數據。")
else:
    sb = sb_raw.copy()
    sb.columns = [c.lower() for c in sb.columns]
    
    all_nba_teams = teams.get_teams()
    id_to_abbr = {t['id']: t['abbreviation'] for t in all_nba_teams}
    
    game_options = []
    game_results = {}

    for _, row in sb.iterrows():
        # 修正：確保 ID 是整數，以利匹配
        h_id = int(row.get('hometeamid', 0))
        a_id = row.get('awayteamid', 0)
        if a_id: a_id = int(a_id)
        
        h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
        
        if h_abbr and a_abbr:
            # 匹配球隊歷史數據
            h_data = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
            a_data = gf[gf['TEAM_ABBREVIATION'] == a_abbr].tail(1)
            
            # 如果還是找不到，嘗試用 TEAM_ID 匹配 (雙保險)
            if h_data.empty: h_data = gf[gf['TEAM_ID'] == h_id].tail(1)
            if a_data.empty: a_data = gf[gf['TEAM_ID'] == a_id].tail(1)
            
            if not h_data.empty and not a_data.empty:
                prob = clf.predict_proba(h_data[feats])[0][1] * 100
                diff = round(abs(float(reg.predict(h_data[feats])[0]) - float(reg.predict(a_data[feats])[0])))
                
                label = f"{TEAM_NAME_CH.get(a_abbr, a_abbr)} @ {TEAM_NAME_CH.get(h_abbr, h_abbr)}"
                game_options.append(label)
                game_results[label] = {
                    'h_name': TEAM_NAME_CH.get(h_abbr, h_abbr),
                    'a_name': TEAM_NAME_CH.get(a_abbr, a_abbr),
                    'h_prob': prob, 'a_prob': 100 - prob,
                    'diff': diff, 'winner': TEAM_NAME_CH.get(h_abbr if prob > 50 else a_abbr)
                }

    if game_options:
        selected = st.selectbox("🎯 選擇場次", game_options)
        res = game_results[selected]
        
        col1, col2, col3 = st.columns(3)
        col1.metric(res['h_name'], f"{res['h_prob']:.1f}%")
        col2.metric(res['a_name'], f"{res['a_prob']:.1f}%")
        col3.metric("預測贏家", res['winner'], f"領先 {res['diff']} 分")
        
        if client:
            if st.button("🪄 生成 AI 專家分析"):
                with st.spinner("AI 分析中..."):
                    p = f"分析 NBA 比賽：{selected}，預測贏家 {res['winner']}，分差 {res['diff']}。請寫 180 字分析。"
                    response = client.models.generate_content(model="gemini-2.0-flash", contents=p)
                    st.info(response.text)
    else:
        st.warning("⚠️ 已抓到賽程，但數據對齊失敗。正在嘗試從備援數據讀取...")
        # 顯示 Debug 資訊方便排錯
        if not sb.empty:
            st.write("API 抓取到的球隊 ID:", sb[['hometeamid', 'awayteamid']].values.tolist())
