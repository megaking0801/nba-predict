import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv3, leaguedashplayerstats
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import pytz, warnings, json
from datetime import datetime
from google import genai

# --- 1. AI & 基本設定 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')

@st.cache_resource
def get_ai_client():
    if "GEMINI_API_KEY" in st.secrets:
        return genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
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

st.set_page_config(page_title="NBA AI v7.5", layout="wide")
st.title("🏀 NBA 數據預測專家")

# --- 2. 獲取基礎模型數據 ---
@st.cache_data(ttl=3600)
def prepare_model():
    # 抓取賽季歷史數據
    gf = leaguegamefinder.LeagueGameFinder(season_nullable='2025-26').get_data_frames()[0]
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    
    # 計算特徵 (近況)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['L10_WIN_RATE'] = gf.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    gf['L5_PTS'] = gf.groupby('TEAM_ID')['PTS'].transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    gf['L5_PLUS_MINUS'] = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    gf['B2B'] = (gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days == 1).astype(int)
    
    feats = ['L5_PTS', 'L5_PLUS_MINUS', 'B2B', 'IS_HOME', 'L10_WIN_RATE']
    train = gf.dropna(subset=feats)
    
    clf = xgb.XGBClassifier().fit(train[feats], train['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(train[feats], train['PLUS_MINUS'])
    
    return clf, reg, gf, feats

clf, reg, gf, feats = prepare_model()

# --- 3. 抓取今日賽程 (正常 API 調用) ---
today_str = datetime.now(tw_tz).strftime('%Y-%m-%d')
sb = scoreboardv3.ScoreboardV3(game_date=today_str).get_data_frames()[0]

if sb.empty:
    st.info(f"📅 {today_str} 目前無比賽數據。")
else:
    # 建立 ID 對應表
    all_teams = teams.get_teams()
    id_to_abbr = {t['id']: t['abbreviation'] for t in all_teams}
    
    game_options = []
    game_results = {}

    for _, row in sb.iterrows():
        h_id, a_id = row['homeTeamId'], row['awayTeamId']
        h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
        
        if h_abbr and a_abbr:
            # 取得兩隊最新數據
            h_data = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
            a_data = gf[gf['TEAM_ABBREVIATION'] == a_abbr].tail(1)
            
            if not h_data.empty and not a_data.empty:
                # 預測勝率
                prob = clf.predict_proba(h_data[feats])[0][1] * 100
                # 預測分差 (保持每次更動：整數)
                diff = round(abs(float(reg.predict(h_data[feats])[0]) - float(reg.predict(a_data[feats])[0])))
                
                label = f"{TEAM_NAME_CH.get(a_abbr, a_abbr)} @ {TEAM_NAME_CH.get(h_abbr, h_abbr)}"
                game_options.append(label)
                game_results[label] = {
                    'h_name': TEAM_NAME_CH.get(h_abbr, h_abbr),
                    'a_name': TEAM_NAME_CH.get(a_abbr, a_abbr),
                    'h_prob': prob,
                    'a_prob': 100 - prob,
                    'diff': diff,
                    'winner': TEAM_NAME_CH.get(h_abbr if prob > 50 else a_abbr)
                }

    if game_options:
        selected = st.selectbox("🎯 選擇場次", game_options)
        res = game_results[selected]
        
        # 顯示預測卡片
        col1, col2, col3 = st.columns(3)
        col1.metric(res['h_name'], f"{res['h_prob']:.1f}%")
        col2.metric(res['a_name'], f"{res['a_prob']:.1f}%")
        col3.metric("預測贏家", res['winner'], f"領先 {res['diff']} 分")
        
        # AI 分析 (保持每次更動：新 SDK)
        if client:
            if st.button("🪄 生成 AI 專家分析"):
                prompt = f"分析 NBA 比賽：{selected}，預測贏家 {res['winner']}，分差 {res['diff']}。請寫 180 字分析。"
                response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
                st.info(response.text)
    else:
        st.warning("抓取到賽程，但模型數據對齊失敗。請稍後重試。")
