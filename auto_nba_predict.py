import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2, scoreboardv3, commonteamroster, leaguedashplayerstats
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import os, json, warnings, pytz
from datetime import datetime, timedelta
import google.generativeai as genai

# --- 1. AI 核心設定 (持續記住：自動偵測模型) ---
@st.cache_resource
def init_ai_v7():
    if "GEMINI_API_KEY" in st.secrets:
        try:
            genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
            available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
            priority_list = ['models/gemini-3-flash', 'models/gemini-3-flash-preview', 
                             'models/gemini-2.5-flash', 'models/gemini-1.5-flash']
            selected_model = next((m for m in priority_list if m in available_models), 'models/gemini-1.5-flash')
            return genai.GenerativeModel(selected_model), selected_model
        except: return None, "Error"
    return None, "No Key"

model_ai, model_name = init_ai_v7()
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
    'POR': '波特蘭開拓者', 'SAC': '沙加邁度國王', 'SAS': '聖安東尼奧馬刺',
    'TOR': '多倫多暴龍', 'UTA': '猶他爵士', 'WAS': '華盛頓巫師'
}

st.set_page_config(page_title="NBA AI 數據專家 v6.7", layout="wide")
st.title("🏀 NBA 終極智慧預測系統 v6.7")

# --- 2. 賽程獲取 (修正 KeyError: 'GAME_ID') ---
@st.cache_data(ttl=600)
def fetch_games_safe(date_str):
    """
    統一 V2 與 V3 的欄位名稱，解決 KeyError
    """
    # 嘗試 V3
    try:
        sb3 = scoreboardv3.ScoreboardV3(game_date=date_str)
        df = sb3.get_data_frames()[0]
        if not df.empty:
            # V3 欄位對照表
            rename_map = {
                'gameId': 'GAME_ID', 
                'homeTeamId': 'HOME_TEAM_ID', 
                'awayTeamId': 'VISITOR_TEAM_ID',
                'gameCode': 'GAME_CODE'
            }
            return df.rename(columns=rename_map).to_dict('records')
    except: pass

    # 嘗試 V2
    try:
        sb2 = scoreboardv2.ScoreboardV2(game_date=date_str)
        df = sb2.get_data_frames()[0]
        if not df.empty:
            return df.to_dict('records')
    except: pass
    
    return []

# --- 3. 持續記住：球員表格精簡 ---
@st.cache_data(ttl=600)
def get_roster_stats(team_id, player_stats_df):
    try:
        ros = commonteamroster.CommonTeamRoster(team_id=team_id, timeout=30).get_data_frames()[0]
        name_col = 'PLAYER_NAME' if 'PLAYER_NAME' in ros.columns else 'PLAYER'
        ros = ros.rename(columns={name_col: 'PLAYER_NAME'})
        merged = ros.merge(player_stats_df, on='PLAYER_NAME', how='left').fillna(0)
        final = merged[['PLAYER_NAME', 'PTS', 'REB', 'AST']].sort_values(by='PTS', ascending=False).head(5)
        final.columns = ['球員姓名', '得分', '籃板', '助攻']
        return final.to_dict('records')
    except: return []

# --- 4. 持續記住：同步生成報告與整數分差 ---
def get_full_analysis(raw_list, clf, reg, gf, ps, feats):
    results = {}; ai_input = {}
    t_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
    
    for g in raw_list:
        # 再次防禦：確保關鍵欄位存在
        g_id = str(g.get('GAME_ID', ''))
        h_id = g.get('HOME_TEAM_ID')
        a_id = g.get('VISITOR_TEAM_ID')
        
        if not g_id or not h_id or not a_id: continue
        
        h_code, a_code = t_map.get(h_id), t_map.get(a_id)
        if not h_code or not a_code: continue
        
        h_f = gf[gf['TEAM_ABBREVIATION'] == h_code].tail(1)
        a_f = gf[gf['TEAM_ABBREVIATION'] == a_code].tail(1)
        if h_f.empty or a_f.empty: continue

        h_in = h_f[feats].copy(); h_in['IS_HOME'] = 1
        a_in = a_f[feats].copy(); a_in['IS_HOME'] = 0
        h_p = (clf.predict_proba(h_in)[:,1][0] / (clf.predict_proba(h_in)[:,1][0] + clf.predict_proba(a_in)[:,1][0])) * 100
        
        # 持續記住：分差整數化
        diff_abs = max(1, round(abs(float(reg.predict(h_in)[0]) - float(reg.predict(a_in)[0]))))
        win_abbr = h_code if h_p > 50 else a_code

        ai_input[g_id] = {'home': TEAM_NAME_CH.get(h_code, h_code), 'away': TEAM_NAME_CH.get(a_code, a_code),
                          'winner': TEAM_NAME_CH.get(win_abbr), 'diff': diff_abs}
        
        results[g_id] = {'h_prob': h_p, 'a_prob': 100-h_p, 'diff': diff_abs, 'win_abbr': win_abbr,
                         'h_name': TEAM_NAME_CH.get(h_code, h_code), 'a_name': TEAM_NAME_CH.get(a_code, a_code),
                         'h_roster': get_roster_stats(h_id, ps), 'a_roster': get_roster_stats(a_id, ps)}

    if ai_input and AI_READY:
        with st.spinner("🧠 AI 同步進行全賽事深度分析..."):
            prompt = f"你是 NBA 專家。請為以下比賽寫 180 字分析，包含整數分差與勝率。回傳 JSON: {{'場次ID': '內容'}}。\n{ai_input}"
            try:
                res = model_ai.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
                reports = json.loads(res.text)
                for gid in results: results[gid]['report'] = reports.get(gid, "報告解析失敗")
            except: 
                for gid in results: results[gid]['report'] = "AI 報告生成中，請稍後刷新。"
    return results

# --- 5. 基礎數據載入 ---
@st.cache_data(ttl=600)
def load_base_data(season):
    try:
        gf = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60).get_data_frames()[0]
        gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
        gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
        gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
        gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
        gf['L10_WIN_RATE'] = gf.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10).mean())
        for c in ['PTS', 'PLUS_MINUS']: gf[f'L5_{c}'] = gf.groupby('TEAM_ID')[c].transform(lambda x: x.shift(1).rolling(5).mean())
        gf['B2B'] = (gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days == 1).astype(int)
        train = gf.dropna(subset=['L5_PTS', 'L10_WIN_RATE'])
        feats = ['L5_PTS', 'L5_PLUS_MINUS', 'B2B', 'IS_HOME', 'L10_WIN_RATE']
        clf = xgb.XGBClassifier().fit(train[feats], train['WIN_BIN'])
        reg = xgb.XGBRegressor().fit(train[feats], train['PLUS_MINUS'])
        ps = leaguedashplayerstats.LeagueDashPlayerStats(season=season, per_mode_detailed='PerGame').get_data_frames()[0]
        return clf, reg, gf, ps[['PLAYER_NAME', 'PTS', 'REB', 'AST']], feats
    except: return None, None, pd.DataFrame(), pd.DataFrame(), []

# --- 6. 頁面渲染 ---
clf, reg, gf, ps, feats = load_base_data('2025-26')
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        d_key = dates[i].strftime('%Y-%m-%d')
        snap_path = f"nba_snapshot_{d_key}.json"
        
        raw_games = fetch_games_safe(dates[i].strftime('%m/%d/%Y'))
        
        if not raw_games and not os.path.exists(snap_path):
            st.warning(f"目前官網無數據或正在更新。日期: {d_key}")
            continue

        if os.path.exists(snap_path):
            with open(snap_path, 'r', encoding='utf-8') as f: data_set = json.load(f)
            st.success("✅ 讀取封存快照")
        else:
            data_set = get_full_analysis(raw_games, clf, reg, gf, ps, feats)
            if data_set and st.button("🔒 封存分析報告", key=f"lock_{d_key}"):
                with open(snap_path, 'w', encoding='utf-8') as f: json.dump(data_set, f, ensure_ascii=False)
                st.rerun()

        if data_set:
            sel_game = st.selectbox("🎯 選擇場次", [f"{v['a_name']} @ {v['h_name']}" for v in data_set.values()], key=f"sel_{d_key}")
            curr_g = next(v for v in data_set.values() if f"{v['a_name']} @ {v['h_name']}" == sel_game)
            
            st.markdown(f"### 🏟️ {sel_game}")
            c1, c2, c3 = st.columns(3)
            c1.metric(f"🏠 {curr_g['h_name']} 勝率", f"{curr_g['h_prob']:.1f}%")
            c2.metric(f"✈️ {curr_g['a_name']} 勝率", f"{curr_g['a_prob']:.1f}%")
            c3.metric("預測贏家", TEAM_NAME_CH.get(curr_g['win_abbr']), delta=f"領先 {curr_g['diff']} 分")
            
            st.info(curr_g.get('report', '暫無分析'))
            
            col_l, col_r = st.columns(2)
            with col_l:
                st.write(f"**{curr_g['h_name']} 核心**")
                st.dataframe(pd.DataFrame(curr_g['h_roster']), hide_index=True)
            with col_r:
                st.write(f"**{curr_g['a_name']} 核心**")
                st.dataframe(pd.DataFrame(curr_g['a_roster']), hide_index=True)
