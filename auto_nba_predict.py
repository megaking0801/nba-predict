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

st.set_page_config(page_title="NBA AI 全時分析 v5.8", layout="wide")
st.title("🏀 NBA 終極智慧預測系統")

# --- 2. 側邊欄 ---
with st.sidebar:
    st.header("🛠️ 系統狀態")
    if AI_READY:
        st.success("Gemini API: 已連線")
    else:
        st.error("Gemini API: 未設定")
    st.info(f"v5.8: Real-time AI Intelligence")

# --- 3. 核心輔助函數 ---
def get_snapshot_path(date_key):
    return f"nba_snapshot_{date_key}.json"

@st.cache_data(ttl=600)
def generate_ai_all_reports(all_games_info):
    """
    即時生成的 AI 報告，透過 Streamlit Cache 減少重複呼叫
    """
    if not AI_READY or not all_games_info:
        return {}

    data_payload = ""
    for g_id, d in all_games_info.items():
        data_payload += f"【場次 {g_id}】{d['away']} @ {d['home']}\n"
        data_payload += f"- 數據: 客勝率 {d['a_wr']:.0f}%, 主勝率 {d['h_wr']:.0f}%\n"
        data_payload += f"- 預測: {d['winner']} 贏 {d['diff']} 分 | B2B: {d['b2b_status']}\n\n"

    prompt = f"""
    你是一位 NBA 大數據球評。請針對以下賽事數據撰寫深度分析：
    {data_payload}

    任務要求：
    1. 每一場分析必須「超過 180 字」。內容要包含戰術、體能與數據對比。
    2. 文中必須明確提到模型預測的「分差（整數）」並解釋其合理性。
    3. 嚴格遵守 JSON 格式：{{"場次ID": "內容", ...}}
    4. 使用台灣繁體中文，語氣犀利專業。
    """
    try:
        response = model_ai.generate_content(
            prompt, 
            generation_config={"response_mime_type": "application/json", "temperature": 0.8}
        )
        return json.loads(response.text)
    except:
        return {}

# --- 4. 數據獲取與模型訓練 ---
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

# --- 5. 預測引擎 ---
def run_prediction(games, clf, reg, all_games_raw, player_stats, features_list):
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
        
        # 勝分差整數化
        raw_diff = float(reg.predict(h_in)[0]) - float(reg.predict(a_in)[0])
        diff_int = max(1, round(abs(raw_diff)))
        winner_side = h_abbr if raw_diff > 0 else a_abbr
        
        ai_input_data[g_id] = {
            'home': TEAM_NAME_CH.get(h_abbr, h_abbr), 'away': TEAM_NAME_CH.get(a_abbr, a_abbr),
            'h_wr': h_feat['L10_WIN_RATE'].values[0]*100, 'a_wr': a_feat['L10_WIN_RATE'].values[0]*100,
            'h_pts': h_feat['L5_PTS'].values[0], 'a_pts': a_feat['L5_PTS'].values[0],
            'b2b_status': f"主隊{'有' if h_feat['B2B'].values[0] else '否'}B2B, 客隊{'有' if a_feat['B2B'].values[0] else '否'}B2B",
            'winner': TEAM_NAME_CH.get(winner_side),
            'diff': diff_int
        }
        
        results[g_id] = {
            'h_prob': h_p, 'a_prob': 100-h_p, 'diff': diff_int,
            'winner_abbr': winner_side,
            'h_team_id': g['HOME_TEAM_ID'], 'a_team_id': g['VISITOR_TEAM_ID'],
            'h_idx': [f"🏠 勝率: {ai_input_data[g_id]['h_wr']:.0f}%", f"🏠 均分: {ai_input_data[g_id]['h_pts']:.1f}"],
            'a_idx': [f"✈️ 勝率: {ai_input_data[g_id]['a_wr']:.0f}%", f"✈️ 均分: {ai_input_data[g_id]['a_pts']:.1f}"]
        }

    # 即時生成 AI 報告
    ai_book = generate_ai_all_reports(ai_input_data)

    final_results = {}
    for g_id, res in results.items():
        def get_roster_data(t_id):
            ros = get_team_roster(t_id)
            if ros.empty or player_stats.empty: return []
            m = ros.merge(player_stats, on='PLAYER_NAME', how='left').fillna(0)
            return m.sort_values(by='PTS', ascending=False).head(5).to_dict('records')

        final_results[g_id] = res
        final_results[g_id]['summary_report'] = ai_book.get(g_id, "AI 分析生成中或暫時無法連線...")
        final_results[g_id]['h_roster'] = get_roster_data(res['h_team_id'])
        final_results[g_id]['a_roster'] = get_roster_data(res['a_team_id'])
        
    return final_results

# --- 6. 介面呈現 ---
clf, reg, all_games_raw, player_stats, features = get_comprehensive_data('2025-26')
date_list = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in date_list])

for i, tab in enumerate(tabs):
    with tab:
        current_date = date_list[i]; date_key = current_date.strftime('%Y-%m-%d')
        games = get_schedule_for_date(current_date); snapshot_file = get_snapshot_path(date_key)
        if not games: st.info("今日無賽程數據"); continue

        # 鎖定狀態判斷
        is_locked = os.path.exists(snapshot_file)
        c_btn, c_txt = st.columns([1, 4])
        
        # 無論是否鎖定，我們都先處理數據
        if is_locked:
            with open(snapshot_file, 'r', encoding='utf-8') as f: ds = json.load(f)
            if c_btn.button("🔓 解鎖 (回復即時更新)", key=f"ul_{date_key}"):
                os.remove(snapshot_file); st.rerun()
            c_txt.success("🔒 目前顯示的是已存檔的快照數據。")
        else:
            ds = run_prediction(games, clf, reg, all_games_raw, player_stats, features)
            if c_btn.button("🔒 鎖定 (存檔此版本)", key=f"lk_{date_key}"):
                with open(snapshot_file, 'w', encoding='utf-8') as f: json.dump(ds, f, ensure_ascii=False)
                st.rerun()
            c_txt.warning("🔄 模式：即時數據與 AI 分析。點擊鎖定可封存今日報告。")

        game_names = [f"{TEAM_NAME_CH.get(g['AWAY_ABBR'], g['AWAY_ABBR'])} @ {TEAM_NAME_CH.get(g['HOME_ABBR'], g['HOME_ABBR'])}" for g in games]
        sel_name = st.selectbox("🎯 選擇場次", options=game_names, key=f"sb_{date_key}")
        
        g_id = str(games[game_names.index(sel_name)]['GAME_ID'])
        res = ds.get(g_id, {})
        
        if res:
            h_n, a_n = TEAM_NAME_CH.get(games[game_names.index(sel_name)]['HOME_ABBR']), TEAM_NAME_CH.get(games[game_names.index(sel_name)]['AWAY_ABBR'])
            st.markdown(f"## 🏟️ {a_n} @ {h_n}")
            
            c1, c2, c3 = st.columns(3)
            c1.metric(f"{h_n} 勝率", f"{float(res.get('h_prob', 0)):.1f}%")
            c2.metric(f"{a_n} 勝率", f"{float(res.get('a_prob', 0)):.1f}%")
            c3.metric("預測贏家", TEAM_NAME_CH.get(res.get('winner_abbr')), delta=f"領先 {res.get('diff')} 分")

            st.write("---")
            st.subheader("📝 AI 深度分析專欄 (即時生成)")
            st.write(res.get('summary_report'))

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
