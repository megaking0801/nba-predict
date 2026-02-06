import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2, leaguedashplayerstats
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import pytz, warnings, json
from datetime import datetime
from google import genai

# --- 1. AI & 基本設定 (持續記住：2026 新 SDK) ---
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

# 保持每次更動：完整中文化
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

st.set_page_config(page_title="NBA AI v5.0 (Stable)", layout="wide")
st.title("🏀 NBA 數據預測專家 v5.0")

# --- 2. 核心數據載入 ---
@st.cache_data(ttl=3600)
def load_base_data():
    # 抓取 2025-26 賽季數據
    gf_raw = leaguegamefinder.LeagueGameFinder(season_nullable='2025-26').get_data_frames()[0]
    nba_ids = [t['id'] for t in teams.get_teams()]
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    
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

clf, reg, gf, feats = load_base_data()

# --- 3. 穩定抓取 ScoreboardV2 ---
today_str = datetime.now(tw_tz).strftime('%m/%d/%Y') # V2 需要的格式
try:
    sb = scoreboardv2.ScoreboardV2(game_date=today_str).get_data_frames()[0]
except:
    sb = pd.DataFrame()

if sb.empty:
    st.info(f"📅 {today_str} 目前無賽程數據。")
else:
    # V2 固定欄位名稱：HOME_TEAM_ID, VISITOR_TEAM_ID
    id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
    game_options = []
    game_results = {}

    for _, row in sb.iterrows():
        h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
        h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
        
        if h_abbr and a_abbr:
            h_data = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
            a_data = gf[gf['TEAM_ABBREVIATION'] == a_abbr].tail(1)
            
            if not h_data.empty and not a_data.empty:
                # 預測
                prob = clf.predict_proba(h_data[feats])[0][1] * 100
                # 每次更動：整數分差
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
        selected = st.selectbox("🎯 選擇今日場次", game_options)
        res = game_results[selected]
        
        col1, col2, col3 = st.columns(3)
        col1.metric(res['h_name'], f"{res['h_prob']:.1f}%")
        col2.metric(res['a_name'], f"{res['a_prob']:.1f}%")
        col3.metric("預測贏家", res['winner'], f"領先 {res['diff']} 分")
        
        # 每次更動：AI 專家分析
        if client:
            if st.button("🪄 生成 AI 深度報告"):
                with st.spinner("AI 分析中..."):
                    prompt = f"分析 NBA 比賽：{selected}，預測贏家 {res['winner']}，分差 {res['diff']}。請寫 180 字分析。"
                    response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
                    st.info(response.text)
    else:
        st.warning("已獲取賽程，但模型數據對齊中。")
