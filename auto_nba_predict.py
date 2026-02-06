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

# --- 1. AI 核心設定 (適配 Gemini 3) ---
def init_ai():
    if "GEMINI_API_KEY" in st.secrets:
        try:
            genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
            # 優先調用 Gemini 3 系列模型，並設定備援邏輯
            # 注意：在 2026 年環境下，Gemini 3 的型號通常標記為 'gemini-3-flash' 或最新版本
            model = genai.GenerativeModel('gemini-3-flash') 
            return model
        except Exception as e:
            # 備援回 1.5 系列，確保系統不崩潰
            try: return genai.GenerativeModel('gemini-1.5-flash')
            except: return None
    return None

model_ai = init_ai()
AI_READY = True if model_ai else False

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
    'POR': '波特蘭開拓者', 'SAC': '沙加緬度國王', 'SAS': '聖安東尼奧馬刺',
    'TOR': '多倫多暴龍', 'UTA': '猶他爵士', 'WAS': '華盛頓巫師'
}

st.set_page_config(page_title="NBA AI 數據專家 v6.4 (Gemini 3)", layout="wide")
st.title("🏀 NBA 終極智慧預測系統 v6.4")

# --- 2. 核心功能函數 ---
def get_snapshot_path(date_key):
    return f"nba_snapshot_{date_key}.json"

@st.cache_data(ttl=600)
def get_team_roster_stats(team_id, player_stats_df):
    """精簡球員表格：僅保留姓名、得分、籃板、助攻"""
    try:
        ros = commonteamroster.CommonTeamRoster(team_id=team_id, timeout=30).get_data_frames()[0]
        if 'PLAYER' in ros.columns:
            ros = ros.rename(columns={'PLAYER': 'PLAYER_NAME'})
        if player_stats_df.empty:
            return ros[['PLAYER_NAME']].head(5).to_dict('records')
        merged = ros.merge(player_stats_df, on='PLAYER_NAME', how='left').fillna(0)
        final = merged[['PLAYER_NAME', 'PTS', 'REB', 'AST']].sort_values(by='PTS', ascending=False).head(5)
        final.columns = ['球員姓名', '得分', '籃板', '助攻']
        return final.to_dict('records')
    except:
        return []

def generate_ai_reports_sync(all_games_info):
    """同步生成：確保 Gemini 3 完整輸出後才顯示"""
    if not AI_READY or not all_games_info: 
        return {g_id: "AI 模型目前無法使用" for g_id in all_games_info}
    
    data_text = ""
    for g_id, d in all_games_info.items():
        data_text += f"ID:{g_id} | {d['away']} vs {d['home']} | 贏家:{d['winner']} | 分差:{d['diff']} | 客勝:{d['a_wr']:.1f}% | 主勝:{d['h_wr']:.1f}%\n"

    prompt = f"""你是一位 NBA 專業分析師（使用 Gemini 3 核心）。請針對以下數據撰寫深度分析報告。
    1. 每場比賽字數「必須超過 180 字」。
    2. 必須提到預測分差（整數）與勝率數據。
    3. 分析需涵蓋戰術對陣、核心球員對位與當前體能狀態。
    4. 回傳嚴格 JSON 格式: {{"場次ID": "分析內容"}}。
    數據內容：\n{data_text}"""
    
    try:
        # Gemini 3 生成配置
        response = model_ai.generate_content(
            prompt, 
            generation_config={"response_mime_type": "application/json", "temperature": 0.8}
        )
        return json.loads(response.text)
    except Exception as e:
        return {g_id: f"Gemini 3 處理異常: {str(e)}" for g_id in all_games_info}

# --- 3. 系統數據載入 ---
@st.cache_data(ttl=600)
def load_all_system_data(season):
    try:
        gf = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60).get_data_frames()[0]
        gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
        gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
        gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
        gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
        gf['L10_WIN_RATE'] = gf.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10).mean())
        for c in ['PTS', 'PLUS_MINUS']:
            gf[f'L5_{c}'] = gf.groupby('TEAM_ID')[c].transform(lambda x: x.shift(1).rolling(5).mean())
        gf['B2B'] = (gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days == 1).astype(int)
        
        train = gf.dropna(subset=['L5_PTS', 'L10_WIN_RATE']).copy()
        feats = ['L5_PTS', 'L5_PLUS_MINUS', 'B2B', 'IS_HOME', 'L10_WIN_RATE']
        clf = xgb.XGBClassifier().fit(train[feats], train['WIN_BIN'])
        reg = xgb.XGBRegressor().fit(train[feats], train['PLUS_MINUS'])
        
        ps = leaguedashplayerstats.LeagueDashPlayerStats(season=season, per_mode_detailed='PerGame').get_data_frames()[0]
        ps = ps[['PLAYER_NAME', 'PTS', 'REB', 'AST']]
        return clf, reg, gf, ps, feats
    except:
        return None, None, pd.DataFrame(), pd.DataFrame(), []

# --- 4. 核心預測流程 (同步化 + 分差整數化) ---
def get_full_analysis(raw_games_list, clf, reg, gf, ps, feats):
    results = {}; ai_input = {}
    t_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
    
    for g in raw_games_list:
        g_id = str(g['GAME_ID'])
        h_id, a_id = g['HOME_TEAM_ID'], g['VISITOR_TEAM_ID']
        h_code, a_code = t_map.get(h_id), t_map.get(a_id)
        
        h_f = gf[gf['TEAM_ABBREVIATION'] == h_code].tail(1)
        a_f = gf[gf['TEAM_ABBREVIATION'] == a_code].tail(1)
        if h_f.empty or a_f.empty: continue

        h_in = h_f[feats].copy(); h_in['IS_HOME'] = 1
        a_in = a_f[feats].copy(); a_in['IS_HOME'] = 0
        
        # 勝率計算
        h_raw_prob = clf.predict_proba(h_in)[:,1][0]
        a_raw_prob = clf.predict_proba(a_in)[:,1][0]
        h_p = (h_raw_prob / (h_raw_prob + a_raw_prob)) * 100
        
        # 勝分差整數化 (最少 1 分)
        raw_diff = float(reg.predict(h_in)[0]) - float(reg.predict(a_in)[0])
        diff_abs = max(1, round(abs(raw_diff)))
        win_abbr = h_code if raw_diff > 0 else a_code

        ai_input[g_id] = {
            'home': TEAM_NAME_CH.get(h_code, h_code), 'away': TEAM_NAME_CH.get(a_code, a_code),
            'h_wr': h_f['L10_WIN_RATE'].values[0]*100, 'a_wr': a_f['L10_WIN_RATE'].values[0]*100,
            'winner': TEAM_NAME_CH.get(win_abbr), 'diff': diff_abs
        }
        results[g_id] = {
            'h_prob': h_p, 'a_prob': 100-h_p, 'diff': diff_abs, 'win_abbr': win_abbr,
            'h_name': TEAM_NAME_CH.get(h_code, h_code), 'a_name': TEAM_NAME_CH.get(a_code, a_code),
            'h_roster': get_team_roster_stats(h_id, ps), 'a_roster': get_team_roster_stats(a_id, ps)
        }
    
    if ai_input:
        with st.spinner("🧠 Gemini 3 正在進行深度戰術分析..."):
            ai_reports = generate_ai_reports_sync(ai_input)
            for g_id in results:
                results[g_id]['report'] = ai_reports.get(g_id, "分析報告獲取失敗")
    return results

# --- 5. UI 渲染 ---
clf, reg, gf, ps, feats = load_all_system_data('2025-26')
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        d_key = dates[i].strftime('%Y-%m-%d')
        snap_path = get_snapshot_path(d_key)
        
        try:
            sb = scoreboardv2.ScoreboardV2(game_date=dates[i].strftime('%m/%d/%Y'), timeout=30)
            raw_games = sb.get_data_frames()[0]
        except: raw_games = pd.DataFrame()

        if raw_games.empty:
            st.info(f"📅 {d_key} 無賽程。")
            continue

        if os.path.exists(snap_path):
            with open(snap_path, 'r', encoding='utf-8') as f: data_set = json.load(f)
            st.success("已載入封存快照數據")
        else:
            data_set = get_full_analysis(raw_games.to_dict('records'), clf, reg, gf, ps, feats)
            if data_set and st.button("🔒 封存分析 (Gemini 3)", key=f"lock_{d_key}"):
                with open(snap_path, 'w', encoding='utf-8') as f: json.dump(data_set, f, ensure_ascii=False)
                st.rerun()

        # 場次選擇
        game_options = [f"{v['a_name']} @ {v['h_name']}" for v in data_set.values()]
        if game_options:
            sel_game = st.selectbox("🎯 選擇場次", game_options, key=f"sel_{d_key}")
            curr_g = [v for v in data_set.values() if f"{v['a_name']} @ {v['h_name']}" == sel_game][0]
            
            st.markdown(f"### 🏟️ {sel_game}")
            c1, c2, c3 = st.columns(3)
            c1.metric(f"🏠 {curr_g['h_name']} 勝率", f"{curr_g['h_prob']:.1f}%")
            c2.metric(f"✈️ {curr_g['a_name']} 勝率", f"{curr_g['a_prob']:.1f}%")
            c3.metric("預測贏家", TEAM_NAME_CH.get(curr_g['win_abbr']), delta=f"領先 {curr_g['diff']} 分")

            st.divider()
            st.subheader("📝 Gemini 3 深度分析")
            st.write(curr_g['report'])

            st.divider()
            st.subheader("👤 核心球員場均戰力")
            col_l, col_r = st.columns(2)
            with col_l:
                st.markdown(f"**{curr_g['h_name']}**")
                st.dataframe(pd.DataFrame(curr_g['h_roster']), hide_index=True, use_container_width=True)
            with col_r:
                st.markdown(f"**{curr_g['a_name']}**")
                st.dataframe(pd.DataFrame(curr_g['a_roster']), hide_index=True, use_container_width=True)
