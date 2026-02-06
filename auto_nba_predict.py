import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2, commonteamroster, leaguedashplayerstats
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import os
import json
from datetime import datetime, timedelta
import pytz
import warnings
import time
import google.generativeai as genai
import random

# --- 1. AI 核心設定 ---
try:
    if "GEMINI_API_KEY" in st.secrets:
        API_KEY = st.secrets["GEMINI_API_KEY"]
        genai.configure(api_key=API_KEY)
        model_ai = genai.GenerativeModel('gemini-1.5-flash')
        AI_READY = True
    else:
        AI_READY = False
except Exception as e:
    AI_READY = False

warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')

TEAM_NAME_CH = {
    'ATL': '亞特蘭大老鷹', 'BKN': '布魯克林籃網', 'BOS': '波士頓塞爾提克',
    'CHA': '夏洛特黃蜂', 'CHI': '芝加哥公牛', 'CLE': '克里夫蘭騎士',
    'DAL': '達拉斯獨行俠', 'DEN': '丹佛金塊', 'DET': '底特律活塞',
    'GSW': '金州勇士', 'HOU': '休士頓火箭', 'IND': '印第安納溜馬',
    'LAC': '洛杉磯快艇', 'LAL': '洛杉磯湖人', 'MEM': '曼非斯灰熊',
    'MIA': '邁阿密熱火', 'MIL': '密爾瓦基公鹿', 'MIN': '明尼蘇達森林狼',
    'NOP': '紐奧良鵜鶘', 'NYK': '紐約尼克', 'OKC': '奧克拉荷馬雷霆',
    'ORL': '奧蘭多魔術', 'PHI': '費城 76 人', 'PHX': '鳳凰城太陽',
    'POR': '波特蘭開拓者', 'SAC': '沙加緬度國王', 'SAS': '聖安東尼奧馬刺',
    'TOR': '多倫多暴龍', 'UTA': '猶他爵士', 'WAS': '華盛頓巫師'
}

st.set_page_config(page_title="NBA AI 穩定版 v5.9", layout="wide")
st.title("🏀 NBA 終極智慧預測系統")

# --- 2. 側邊欄 ---
with st.sidebar:
    st.header("🛠️ 系統狀態")
    if AI_READY:
        st.success("Gemini API: 已連線")
    else:
        st.error("Gemini API: 未設定")
    st.info(f"v5.9: Connection Shield & Daily Fix")

# --- 3. 核心輔助函數 ---
def get_snapshot_path(date_key):
    return f"nba_snapshot_{date_key}.json"

@st.cache_data(ttl=600)
def generate_ai_all_reports(all_games_info):
    if not AI_READY or not all_games_info:
        return {}
    data_payload = ""
    for g_id, d in all_games_info.items():
        data_payload += f"【場次 {g_id}】{d['away']} @ {d['home']}\n"
        data_payload += f"- 數據: 客勝率 {d['a_wr']:.0f}%, 主勝率 {d['h_wr']:.0f}%\n"
        data_payload += f"- 預測: {d['winner']} 贏 {d['diff']} 分 | B2B: {d['b2b_status']}\n\n"

    prompt = f"你是一位 NBA 大數據球評。請針對以下賽事數據撰寫超過 180 字的深度戰術分析報告（台灣繁體中文）。必須解釋為何預測分差為該整數。格式為 JSON：{data_payload}"
    try:
        response = model_ai.generate_content(prompt, generation_config={"response_mime_type": "application/json", "temperature": 0.8})
        return json.loads(response.text)
    except:
        return {}

# --- 4. 數據獲取 (強化版) ---
@st.cache_data(ttl=600)
def get_comprehensive_data(season):
    all_games = pd.DataFrame()
    for i in range(3):
        try:
            gamefinder = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60)
            all_games = gamefinder.get_data_frames()[0]
            if not all_games.empty: break
        except: time.sleep(2)
    if all_games.empty: return None, None, pd.DataFrame(), pd.DataFrame(), []
    
    all_games['GAME_DATE'] = pd.to_datetime(all_games['GAME_DATE'])
    all_games = all_games.sort_values(['TEAM_ID', 'GAME_DATE'])
    all_games['IS_HOME'] = all_games['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    all_games['WIN_BIN'] = all_games['WL'].apply(lambda x: 1 if x == 'W' else 0)
    all_games['L10_WIN_RATE'] = all_games.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10).mean())
    stats_cols = ['PTS', 'PLUS_MINUS', 'FG_PCT']
    for col in stats_cols:
        all_games[f'L5_{col}'] = all_games.groupby('TEAM_ID')[col].transform(lambda x: x.shift(1).rolling(5).mean())
    all_games['B2B'] = (all_games.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days == 1).astype(int)
    
    train_df = all_games.dropna(subset=['L5_PTS', 'L10_WIN_RATE']).copy()
    features = [f'L5_{c}' for c in stats_cols] + ['B2B', 'IS_HOME', 'L10_WIN_RATE']
    clf = xgb.XGBClassifier(n_estimators=100, max_depth=3, learning_rate=0.1)
    clf.fit(train_df[features], train_df['WIN_BIN'])
    reg = xgb.XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.1)
    reg.fit(train_df[features], train_df['PLUS_MINUS'])
    
    try:
        p_stats = leaguedashplayerstats.LeagueDashPlayerStats(season=season, per_mode_detailed='PerGame').get_data_frames()[0]
        player_stats = p_stats[['PLAYER_NAME', 'TEAM_ID', 'PTS', 'REB', 'AST']]
    except: player_stats = pd.DataFrame()
    return clf, reg, all_games, player_stats, features

@st.cache_data(ttl=3600)
def get_schedule_for_date(date_obj):
    """強化版賽程抓取：包含重試機制與備用方案"""
    date_str = date_obj.strftime('%m/%d/%Y')
    t_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
    
    # 方案 A: ScoreboardV2
    for _ in range(2):
        try:
            sb = scoreboardv2.ScoreboardV2(game_date=date_str, timeout=30)
            df = sb.get_data_frames()[0]
            if not df.empty:
                df['HOME_ABBR'] = df['HOME_TEAM_ID'].map(t_map)
                df['AWAY_ABBR'] = df['VISITOR_TEAM_ID'].map(t_map)
                return df.to_dict('records')
        except: time.sleep(1)
    return []

# --- 5. 預測引擎 ---
def run_prediction(games, clf, reg, all_games_raw, player_stats, features_list):
    results = {}; ai_input_data = {}
    for g in games:
        g_id = str(g['GAME_ID'])
        h_abbr, a_abbr = g.get('HOME_ABBR'), g.get('AWAY_ABBR')
        if not h_abbr or not a_abbr: continue
        
        h_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == h_abbr].tail(1)
        a_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == a_abbr].tail(1)
        if h_feat.empty or a_feat.empty: continue

        h_in = h_feat[features_list].copy(); h_in['IS_HOME'] = 1
        a_in = a_feat[features_list].copy(); a_in['IS_HOME'] = 0
        
        h_p = (clf.predict_proba(h_in)[:, 1][0] / (clf.predict_proba(h_in)[:, 1][0] + clf.predict_proba(a_in)[:, 1][0])) * 100
        raw_diff = float(reg.predict(h_in)[0]) - float(reg.predict(a_in)[0])
        diff_int = max(1, round(abs(raw_diff)))
        winner_side = h_abbr if raw_diff > 0 else a_abbr
        
        ai_input_data[g_id] = {
            'home': TEAM_NAME_CH.get(h_abbr, h_abbr), 'away': TEAM_NAME_CH.get(a_abbr, a_abbr),
            'h_wr': h_feat['L10_WIN_RATE'].values[0]*100, 'a_wr': a_feat['L10_WIN_RATE'].values[0]*100,
            'h_pts': h_feat['L5_PTS'].values[0], 'a_pts': a_feat['L5_PTS'].values[0],
            'b2b_status': f"主隊{'有' if h_feat['B2B'].values[0] else '否'}B2B",
            'winner': TEAM_NAME_CH.get(winner_side), 'diff': diff_int
        }
        results[g_id] = {
            'h_prob': h_p, 'a_prob': 100-h_p, 'diff': diff_int, 'winner_abbr': winner_side,
            'h_team_id': g['HOME_TEAM_ID'], 'a_team_id': g['VISITOR_TEAM_ID'],
            'h_idx': [f"🏠 勝率: {ai_input_data[g_id]['h_wr']:.0f}%", f"🏠 均分: {ai_input_data[g_id]['h_pts']:.1f}"],
            'a_idx': [f"✈️ 勝率: {ai_input_data[g_id]['a_wr']:.0f}%", f"✈️ 均分: {ai_input_data[g_id]['a_pts']:.1f}"]
        }
    ai_book = generate_ai_all_reports(ai_input_data)
    for g_id in results:
        results[g_id]['summary_report'] = ai_book.get(g_id, "AI 分析生成中...")
    return results

# --- 6. UI ---
clf, reg, all_games_raw, player_stats, features = get_comprehensive_data('2025-26')
date_list = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in date_list])

for i, tab in enumerate(tabs):
    with tab:
        current_date = date_list[i]; date_key = current_date.strftime('%Y-%m-%d')
        games = get_schedule_for_date(current_date); snapshot_file = get_snapshot_path(date_key)
        
        if not games:
            st.warning(f"⚠️ {date_key} 找不到賽程資料。請確認今日是否有比賽，或嘗試刷新網頁。")
            if st.button("🔄 強制刷新賽程", key=f"re_{date_key}"): st.rerun()
            continue

        is_locked = os.path.exists(snapshot_file)
        c_btn, c_txt = st.columns([1, 4])
        
        if is_locked:
            with open(snapshot_file, 'r', encoding='utf-8') as f: ds = json.load(f)
            if c_btn.button("🔓 解鎖", key=f"ul_{date_key}"): os.remove(snapshot_file); st.rerun()
        else:
            ds = run_prediction(games, clf, reg, all_games_raw, player_stats, features)
            if c_btn.button("🔒 鎖定", key=f"lk_{date_key}"):
                with open(snapshot_file, 'w', encoding='utf-8') as f: json.dump(ds, f, ensure_ascii=False); st.rerun()

        game_names = [f"{TEAM_NAME_CH.get(g.get('AWAY_ABBR'), '客隊')} @ {TEAM_NAME_CH.get(g.get('HOME_ABBR'), '主隊')}" for g in games]
        sel_name = st.selectbox("🎯 選擇場次", options=game_names, key=f"sb_{date_key}")
        
        idx = game_names.index(sel_name)
        g_id = str(games[idx]['GAME_ID'])
        res = ds.get(g_id)
        
        if res:
            st.markdown(f"## 🏟️ {sel_name}")
            c1, c2, c3 = st.columns(3)
            c1.metric("主隊勝率", f"{res['h_prob']:.1f}%")
            c2.metric("客隊勝率", f"{res['a_prob']:.1f}%")
            c3.metric("預測贏家", TEAM_NAME_CH.get(res['winner_abbr']), delta=f"領先 {res['diff']} 分")
            st.info(res['summary_report'])
