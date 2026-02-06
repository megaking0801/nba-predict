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

st.set_page_config(page_title="NBA AI 數據深度分析 v5.6", layout="wide")
st.title("🏀 NBA 終極智慧預測系統")

# --- 2. 側邊欄 ---
with st.sidebar:
    st.header("🛠️ 系統狀態")
    if AI_READY:
        st.success("Gemini API: 已連線")
    else:
        st.error("Gemini API: 未設定")
    st.info(f"v5.6: Data-Driven deep Analysis")

# --- 3. 核心輔助函數 ---
def get_snapshot_path(date_key):
    return f"nba_snapshot_{date_key}.json"

def generate_ai_all_reports(all_games_info):
    """
    一次將所有場次數據丟給 AI，並強迫其分析具體數值且字數達標
    """
    if not AI_READY or not all_games_info:
        return {}

    # 構建更詳細的數據摘要
    data_payload = ""
    for g_id, d in all_games_info.items():
        data_payload += f"【場次 ID:{g_id}】{d['away']} (客) @ {d['home']} (主)\n"
        data_payload += f"- 數據指標: 客隊勝率 {d['a_wr']:.0f}%, 主隊勝率 {d['h_wr']:.0f}%\n"
        data_payload += f"- 火力表現: 客隊近五場均分 {d['a_pts']:.1f}, 主隊 {d['h_pts']:.1f}\n"
        data_payload += f"- 體能狀況: {d['b2b_status']}\n"
        data_payload += f"- 模型預測: {d['winner']} 贏 {d['diff']} 分\n\n"

    prompt = f"""
    你是一位精通大數據分析的 NBA 資深球評。以下是今日比賽的真實預測數據：
    {data_payload}

    任務：請為上述「每一場」比賽撰寫一份深度分析報告。
    
    【報告要求 - 嚴格執行】：
    1. 字數限制：每場比賽的分析內容不得少於 150 字，必須內容紮實。
    2. 必須引用數據：報告中必須具體提到我給你的「勝率百分比」或「場均得分差」或「B2B體能狀況」。
    3. 分析結構：
       - 第一段：針對兩隊目前的火力差與勝率進行對比。
       - 第二段：分析體能（B2B）或主場優勢對這場比賽的影響。
       - 第三段：總結模型為何看好 {all_games_info[list(all_games_info.keys())[0]]['winner'] if all_games_info else '預測方'} 獲勝的關鍵 X 因素。
    4. 語言：台灣繁體中文，語氣要專業、犀利，像專業運動專欄。
    5. 禁止：禁止使用「火力穩定」、「表現出色」等空洞詞彙，請改用具體數值分析。
    6. 格式：回傳嚴格的 JSON 格式，鍵值為場次 ID，內容為分析字串。
    """
    
    try:
        response = model_ai.generate_content(
            prompt, 
            generation_config={
                "response_mime_type": "application/json", 
                "temperature": 0.9,
                "top_p": 0.95
            }
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"AI Error: {e}")
        return {}

# --- 4. 數據獲取與模型 ---
@st.cache_data(ttl=600)
def get_comprehensive_data(season):
    all_games = pd.DataFrame()
    player_stats = pd.DataFrame()
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
    except: pass
    return clf, reg, all_games, player_stats, features

@st.cache_data(ttl=600)
def get_team_roster(team_id):
    try:
        roster = commonteamroster.CommonTeamRoster(team_id=team_id, timeout=30).get_data_frames()[0]
        if 'PLAYER' in roster.columns: roster = roster.rename(columns={'PLAYER': 'PLAYER_NAME'})
        return roster[['PLAYER_NAME']]
    except: return pd.DataFrame(columns=['PLAYER_NAME'])

@st.cache_data(ttl=3600)
def get_schedule_for_date(date_obj):
    date_str = date_obj.strftime('%m/%d/%Y')
    try:
        sb = scoreboardv2.ScoreboardV2(game_date=date_str, timeout=30)
        df = sb.get_data_frames()[0]
        t_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        if not df.empty:
            df['HOME_ABBR'] = df['HOME_TEAM_ID'].map(t_map)
            df['AWAY_ABBR'] = df['VISITOR_TEAM_ID'].map(t_map)
            return df.to_dict('records')
    except: pass
    return []

# --- 5. 預測與分析引擎 ---
def run_prediction(games, clf, reg, all_games_raw, player_stats, features_list, is_snapshot=False):
    results = {}
    ai_input_data = {}
    
    for g in games:
        g_id = str(g['GAME_ID'])
        h_abbr, a_abbr = g['HOME_ABBR'], g['AWAY_ABBR']
        h_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == h_abbr].tail(1)
        a_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == a_abbr].tail(1)
        if h_feat.empty or a_feat.empty: continue

        h_in = h_feat[features_list].copy(); h_in['IS_HOME'] = 1
        a_in = a_feat[features_list].copy(); a_in['IS_HOME'] = 0
        
        h_p_raw = clf.predict_proba(h_in)[:, 1][0]
        a_p_raw = clf.predict_proba(a_in)[:, 1][0]
        h_p = (float(h_p_raw) / (float(h_p_raw) + float(a_p_raw))) * 100
        diff = round(float(reg.predict(h_in)[0]) - float(reg.predict(a_in)[0]), 1)
        
        h_n_ch, a_n_ch = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
        
        # 準備 AI 輸入數據
        ai_input_data[g_id] = {
            'home': h_n_ch, 'away': a_n_ch,
            'h_wr': h_feat['L10_WIN_RATE'].values[0]*100, 'a_wr': a_feat['L10_WIN_RATE'].values[0]*100,
            'h_pts': h_feat['L5_PTS'].values[0], 'a_pts': a_feat['L5_PTS'].values[0],
            'b2b_status': f"主隊{'有B2B體能壓力' if h_feat['B2B'].values[0] else '體能正常'}, 客隊{'有B2B體能壓力' if a_feat['B2B'].values[0] else '體能正常'}",
            'winner': h_n_ch if diff > 0 else a_n_ch,
            'diff': abs(diff)
        }
        
        results[g_id] = {
            'h_prob': h_p, 'a_prob': 100-h_p, 'diff': diff,
            'winner_abbr': h_abbr if diff > 0 else a_abbr,
            'h_idx': [f"🏠 勝率: {ai_input_data[g_id]['h_wr']:.0f}%", f"🏠 均分: {ai_input_data[g_id]['h_pts']:.1f}"],
            'a_idx': [f"✈️ 勝率: {ai_input_data[g_id]['a_wr']:.0f}%", f"✈️ 均分: {ai_input_data[g_id]['a_pts']:.1f}"],
            'h_team_id': g['HOME_TEAM_ID'], 'a_team_id': g['VISITOR_TEAM_ID']
        }

    # 執行 AI 分析
    ai_book = {}
    if is_snapshot and ai_input_data:
        with st.spinner("🔍 正在進行深度大數據分析，請稍候..."):
            ai_book = generate_ai_all_reports(ai_input_data)

    final_results = {}
    for g_id, res in results.items():
        def get_roster_data(t_id):
            ros = get_team_roster(t_id)
            if ros.empty or player_stats.empty: return []
            m = ros.merge(player_stats, on='PLAYER_NAME', how='left').fillna(0)
            return m.sort_values(by='PTS', ascending=False).head(5).to_dict('records')

        final_results[g_id] = res
        # 字數檢查警告
        report_content = ai_book.get(g_id, "")
        if report_content and len(report_content) < 100:
            report_content += "\n\n(註：AI 生成內容較短，建議解鎖後重新鎖定以獲取更完整分析。)"
            
        final_results[g_id]['summary_report'] = report_content if report_content else f"【數據快評】模型看好 {TEAM_NAME_CH.get(res['winner_abbr'])}，預計勝率差達 {abs(res['h_prob']-res['a_prob']):.1f}%。請鎖定數據以生成深度報告。"
        final_results[g_id]['h_roster'] = get_roster_data(res['h_team_id'])
        final_results[g_id]['a_roster'] = get_roster_data(res['a_team_id'])
        
    return final_results

# --- 6. UI ---
clf, reg, all_games_raw, player_stats, features = get_comprehensive_data('2025-26')
date_list = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in date_list])

for i, tab in enumerate(tabs):
    with tab:
        current_date = date_list[i]; date_key = current_date.strftime('%Y-%m-%d')
        games = get_schedule_for_date(current_date); snapshot_file = get_snapshot_path(date_key)
        if not games: st.info("今日暫無賽程數據"); continue

        is_locked = os.path.exists(snapshot_file)
        c_btn, c_txt = st.columns([1, 4])
        
        if not is_locked:
            if c_btn.button("🔒 鎖定並生成深度報告", key=f"lk_{date_key}"):
                ld = run_prediction(games, clf, reg, all_games_raw, player_stats, features, is_snapshot=True)
                with open(snapshot_file, 'w', encoding='utf-8') as f: json.dump(ld, f, ensure_ascii=False)
                st.rerun()
            c_txt.warning("目前為即時模式。點擊左側鎖定按鈕，AI 將根據全量數據產出至少 150 字的專業分析。")
        else:
            if c_btn.button("🔓 解鎖重新分析", key=f"ul_{date_key}"):
                os.remove(snapshot_file); st.rerun()
            c_txt.success("數據已鎖定，顯示深度分析報告中。")

        game_names = [f"{TEAM_NAME_CH.get(g['AWAY_ABBR'], g['AWAY_ABBR'])} @ {TEAM_NAME_CH.get(g['HOME_ABBR'], g['HOME_ABBR'])}" for g in games]
        sel_name = st.selectbox("🎯 選擇對戰場次", options=game_names, key=f"sb_{date_key}")
        
        if is_locked:
            with open(snapshot_file, 'r', encoding='utf-8') as f: ds = json.load(f)
        else:
            ds = run_prediction(games, clf, reg, all_games_raw, player_stats, features, is_snapshot=False)

        g_id = str(games[game_names.index(sel_name)]['GAME_ID'])
        res = ds.get(g_id, {})
        
        if res:
            h_n, a_n = TEAM_NAME_CH.get(games[game_names.index(sel_name)]['HOME_ABBR']), TEAM_NAME_CH.get(games[game_names.index(sel_name)]['AWAY_ABBR'])
            st.markdown(f"## 🏟️ {a_n} @ {h_n}")
            
            c1, c2, c3 = st.columns(3)
            c1.metric(f"{h_n} 勝率", f"{float(res.get('h_prob', 0)):.1f}%")
            c2.metric(f"{a_n} 勝率", f"{float(res.get('a_prob', 0)):.1f}%")
            c3.metric("預測贏家", TEAM_NAME_CH.get(res.get('winner_abbr')), delta=f"領先 {abs(float(res.get('diff', 0)))} 分")

            st.write("---")
            st.subheader("📝 AI 深度分析專欄 (大數據驅動)")
            # 這裡會顯示達標的 150 字以上分析
            st.write(res.get('summary_report', "分析生成中..."))

            l_col, r_col = st.columns(2)
            with l_col:
                st.markdown(f"**🏠 {h_n} 指標**")
                for item in res.get('h_idx', []): st.write(item)
            with r_col:
                st.markdown(f"**✈️ {a_n} 指標**")
                for item in res.get('a_idx', []): st.write(item)

            st.write("---")
            st.subheader("👤 核心球員數據")
            def safe_df(data):
                df = pd.DataFrame(data if data else [])
                return df[['PLAYER_NAME','PTS','REB','AST']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分','REB':'籃板','AST':'助攻'}) if not df.empty else pd.DataFrame()
            cl, cr = st.columns(2)
            cl.dataframe(safe_df(res.get('h_roster')), hide_index=True, use_container_width=True)
            cr.dataframe(safe_df(res.get('a_roster')), hide_index=True, use_container_width=True)
