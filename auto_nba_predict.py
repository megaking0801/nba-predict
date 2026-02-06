import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv3, commonteamroster, leaguedashplayerstats
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import os, json, warnings, pytz
from datetime import datetime, timedelta
from google import genai
from google.genai import types

# --- 1. AI 設定 (持續記住：2026 新 SDK) ---
@st.cache_resource
def init_ai_v74():
    if "GEMINI_API_KEY" in st.secrets:
        try:
            client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
            return client, "gemini-2.0-flash"
        except: return None, "AI ERROR"
    return None, "No API Key"

client_ai, model_id = init_ai_v74()
tw_tz = pytz.timezone('Asia/Taipei')
warnings.filterwarnings('ignore')

# 每次更動：核心球隊 ID 字典 (防止 ID 匹配失敗)
TEAM_ID_MAP = {
    1610612737: 'ATL', 1610612738: 'BOS', 1610612739: 'CLE', 1610612740: 'NOP',
    1610612741: 'CHI', 1610612742: 'DAL', 1610612743: 'DEN', 1610612744: 'GSW',
    1610612745: 'HOU', 1610612746: 'LAC', 1610612747: 'LAL', 1610612748: 'MIA',
    1610612749: 'MIL', 1610612750: 'MIN', 1610612751: 'BKN', 1610612752: 'NYK',
    1610612753: 'ORL', 1610612754: 'IND', 1610612755: 'PHI', 1610612756: 'PHX',
    1610612757: 'POR', 1610612758: 'SAC', 1610612759: 'SAS', 1610612760: 'OKC',
    1610612761: 'TOR', 1610612762: 'UTA', 1610612763: 'MEM', 1610612764: 'WAS',
    1610612765: 'DET', 1610612766: 'CHA'
}

TEAM_NAME_CH = {
    'ATL': '亞特蘭大老鷹', 'BKN': '布魯克林籃網', 'BOS': '波士頓塞爾提克',
    'CHA': '夏洛特黃蜂', 'CHI': '芝加哥公牛', 'CLE': '克里夫蘭騎士',
    'DAL': '達拉斯獨行俠', 'DEN': '丹佛金塊', 'DET': '底特律活塞',
    'GSW': '金州勇勇士', 'HOU': '休士頓火箭', 'IND': '印第安納溜馬',
    'LAC': '洛杉磯快艇', 'LAL': '洛杉磯湖人', 'MEM': '曼非斯灰熊',
    'MIA': '邁阿密熱火', 'MIL': '密爾瓦基公鹿', 'MIN': '明尼蘇達灰狼',
    'NOP': '紐奧良鵜鶘', 'NYK': '紐約尼克', 'OKC': '奧克拉荷馬雷霆',
    'ORL': '奧蘭多魔術', 'PHI': '費城 76 人', 'PHX': '鳳凰城太陽',
    'POR': '波特蘭開拓者', 'SAC': '沙加邁度國王', 'SAS': '聖安東尼奧馬刺',
    'TOR': '多倫多暴龍', 'UTA': '猶他爵士', 'WAS': '華盛頓巫師'
}

st.set_page_config(page_title="NBA AI 專家 v7.4", layout="wide")
st.title("🏀 NBA 終極智慧預測系統 v7.4")

# --- 2. 數據庫加載 ---
@st.cache_data(ttl=600)
def load_base_data(season):
    try:
        gf = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60).get_data_frames()[0]
        gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
        gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
        gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
        gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
        gf['L10_WIN_RATE'] = gf.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
        for c in ['PTS', 'PLUS_MINUS']: 
            gf[f'L5_{c}'] = gf.groupby('TEAM_ID')[c].transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
        gf['B2B'] = (gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days == 1).astype(int)
        
        train = gf.fillna(0)
        feats = ['L5_PTS', 'L5_PLUS_MINUS', 'B2B', 'IS_HOME', 'L10_WIN_RATE']
        clf = xgb.XGBClassifier().fit(train[feats], train['WIN_BIN'])
        reg = xgb.XGBRegressor().fit(train[feats], train['PLUS_MINUS'])
        ps = leaguedashplayerstats.LeagueDashPlayerStats(season=season, per_mode_detailed='PerGame').get_data_frames()[0]
        return clf, reg, gf, ps[['PLAYER_NAME', 'PTS', 'REB', 'AST']], feats, None
    except Exception as e: return None, None, None, None, [], str(e)

clf, reg, gf, ps, feats, error_msg = load_base_data('2025-26')
if error_msg:
    st.error(f"⚠️ 數據載入失敗: {error_msg}")
    st.stop()

# --- 3. 賽程獲取 ---
def fetch_games_v74(d_obj):
    d_v3 = d_obj.strftime('%Y-%m-%d')
    try:
        sb3 = scoreboardv3.ScoreboardV3(game_date=d_v3, timeout=30).get_data_frames()[0]
        if not sb3.empty:
            return sb3.rename(columns={'gameId': 'GAME_ID', 'homeTeamId': 'HOME_TEAM_ID', 'awayTeamId': 'VISITOR_TEAM_ID'}).to_dict('records')
    except: pass
    return []

# --- 4. 核心分析 (修復數據匹配) ---
def analyze_v74(raw_list, clf, reg, gf, ps, feats):
    results = {}
    for g in raw_list:
        g_id = str(g.get('GAME_ID', ''))
        h_id, a_id = g.get('HOME_TEAM_ID'), g.get('VISITOR_TEAM_ID')
        
        # 修正點：使用手動映射表，避開動態抓取失敗
        h_code = TEAM_ID_MAP.get(h_id)
        a_code = TEAM_ID_MAP.get(a_id)
        
        if not h_code or not a_code: continue
        
        h_f = gf[gf['TEAM_ABBREVIATION'] == h_code].sort_values('GAME_DATE').tail(1)
        a_f = gf[gf['TEAM_ABBREVIATION'] == a_code].sort_values('GAME_DATE').tail(1)
        
        if h_f.empty or a_f.empty: continue

        h_p_raw = clf.predict_proba(h_f[feats])[:,1][0]
        a_p_raw = clf.predict_proba(a_f[feats])[:,1][0]
        h_p = (h_p_raw / (h_p_raw + a_p_raw)) * 100
        
        # 每次更動：整數分差
        diff_abs = max(1, round(abs(float(reg.predict(h_f[feats])[0]) - float(reg.predict(a_f[feats])[0]))))
        win_abbr = h_code if h_p > 50 else a_code

        results[g_id] = {
            'h_prob': h_p, 'a_prob': 100-h_p, 'diff': diff_abs, 'win_abbr': win_abbr,
            'h_name': TEAM_NAME_CH.get(h_code, h_code), 'a_name': TEAM_NAME_CH.get(a_code, a_code),
            'h_roster': ps[ps['PLAYER_NAME'].isin([])].to_dict('records'), # 簡化防崩潰
            'a_roster': ps[ps['PLAYER_NAME'].isin([])].to_dict('records')
        }
    return results

# --- 5. 渲染 ---
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        d_obj = dates[i]
        d_key = d_obj.strftime('%Y-%m-%d')
        snap_path = f"nba_snapshot_{d_key}.json"
        
        raw_games = fetch_games_v74(d_obj)
        if not raw_games:
            st.info("無賽程更新。")
        else:
            data_set = analyze_v74(raw_games, clf, reg, gf, ps, feats)
            if data_set:
                sel = st.selectbox("🎯 選擇場次", [f"{v['a_name']} @ {v['h_name']}" for v in data_set.values()], key=f"s_{d_key}")
                curr = next(v for v in data_set.values() if f"{v['a_name']} @ {v['h_name']}" == sel)
                
                st.markdown(f"### 🏟️ {sel}")
                c1, c2, c3 = st.columns(3)
                c1.metric(f"🏠 {curr['h_name']}", f"{curr['h_prob']:.1f}%")
                c2.metric(f"✈️ {curr['a_name']}", f"{curr['a_prob']:.1f}%")
                c3.metric("預測勝方", TEAM_NAME_CH.get(curr['win_abbr']), delta=f"領先 {curr['diff']} 分")
                
                # AI 快速分析
                if client_ai and st.button("🪄 生成 AI 深度報告", key=f"ai_{d_key}_{sel}"):
                    with st.spinner("AI 思考中..."):
                        p = f"分析 NBA 比賽: {curr['a_name']} 對戰 {curr['h_name']}，預測贏家 {TEAM_NAME_CH.get(curr['win_abbr'])}，分差 {curr['diff']}。寫180字分析。"
                        res = client_ai.models.generate_content(model=model_id, contents=p)
                        st.info(res.text)
            else:
                st.warning("數據庫對應失敗，請檢查 API 回傳 ID。")
