import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2, scoreboardv3, commonteamroster, leaguedashplayerstats
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import os, json, warnings, pytz
from datetime import datetime, timedelta

# --- 1. AI 核心設定 (適配 2026 google-genai SDK) ---
try:
    from google import genai
    from google.genai import types
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

@st.cache_resource
def init_ai_v9():
    if not SDK_AVAILABLE:
        return None, "requirements.txt 缺少 google-genai"
    if "GEMINI_API_KEY" in st.secrets:
        try:
            client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
            # 自動偵測模型 (優先使用 Gemini 2.0/3.0 系列)
            model_id = "gemini-2.0-flash" 
            return client, model_id
        except Exception as e:
            return None, f"AI 初始化失敗: {str(e)}"
    return None, "未偵測到 GEMINI_API_KEY"

client_ai, model_id = init_ai_v9()
AI_READY = True if client_ai and isinstance(client_ai, genai.Client) else False
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')

# 每次更動：保持完整中文化隊名
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

st.set_page_config(page_title="NBA AI 數據專家 v6.9", layout="wide")
st.title("🏀 NBA 終極智慧預測系統 v6.9")

# --- 2. 核心穩定數據載入 ---
@st.cache_data(ttl=600)
def load_base_data(season):
    try:
        gf = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=45).get_data_frames()[0]
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
        return clf, reg, gf, ps[['PLAYER_NAME', 'PTS', 'REB', 'AST']], feats, None
    except Exception as e:
        return None, None, pd.DataFrame(), pd.DataFrame(), [], str(e)

clf, reg, gf, ps, feats, error_msg = load_base_data('2025-26')
if error_msg:
    st.error(f"❌ 數據載入失敗，請稍後刷新: {error_msg}")
    st.stop()

# --- 3. 賽程獲取 (修正 KeyError 與 V3 相容) ---
@st.cache_data(ttl=600)
def fetch_games_safe(date_str):
    try:
        sb3 = scoreboardv3.ScoreboardV3(game_date=date_str)
        df = sb3.get_data_frames()[0]
        if not df.empty:
            return df.rename(columns={'gameId': 'GAME_ID', 'homeTeamId': 'HOME_TEAM_ID', 'awayTeamId': 'VISITOR_TEAM_ID'}).to_dict('records')
    except: pass
    try:
        sb2 = scoreboardv2.ScoreboardV2(game_date=date_str)
        df = sb2.get_data_frames()[0]
        if not df.empty: return df.to_dict('records')
    except: pass
    return []

# --- 4. 球員表格精簡 (每次更動：PTS, REB, AST) ---
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

# --- 5. 同步分析邏輯 (新 SDK + 整數分差) ---
def get_full_analysis(raw_list, clf, reg, gf, ps, feats):
    results = {}; ai_input = {}
    t_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
    for g in raw_list:
        g_id = str(g.get('GAME_ID', ''))
        h_id, a_id = g.get('HOME_TEAM_ID'), g.get('VISITOR_TEAM_ID')
        if not g_id or not h_id or not a_id: continue
        h_code, a_code = t_map.get(h_id), t_map.get(a_id)
        h_f, a_f = gf[gf['TEAM_ABBREVIATION'] == h_code].tail(1), gf[gf['TEAM_ABBREVIATION'] == a_code].tail(1)
        if h_f.empty or a_f.empty: continue

        h_p = (clf.predict_proba(h_f[feats])[:,1][0] / (clf.predict_proba(h_f[feats])[:,1][0] + clf.predict_proba(a_f[feats])[:,1][0])) * 100
        # 每次更動：分差整數化
        diff_abs = max(1, round(abs(float(reg.predict(h_f[feats])[0]) - float(reg.predict(a_f[feats])[0]))))
        win_abbr = h_code if h_p > 50 else a_code

        ai_input[g_id] = {'home': TEAM_NAME_CH.get(h_code, h_code), 'away': TEAM_NAME_CH.get(a_code, a_code),
                          'winner': TEAM_NAME_CH.get(win_abbr), 'diff': diff_abs}
        results[g_id] = {'h_prob': h_p, 'a_prob': 100-h_p, 'diff': diff_abs, 'win_abbr': win_abbr,
                         'h_name': TEAM_NAME_CH.get(h_code, h_code), 'a_name': TEAM_NAME_CH.get(a_code, a_code),
                         'h_roster': get_roster_stats(h_id, ps), 'a_roster': get_roster_stats(a_id, ps)}

    if ai_input and AI_READY:
        with st.spinner("🧠 AI 同步進行全賽事深度分析..."):
            prompt = f"你是 NBA 專家。請為以下比賽寫 180 字以上分析，包含整數分差與勝率。回傳 JSON: {{'場次ID': '內容'}}。\n{ai_input}"
            try:
                response = client_ai.models.generate_content(
                    model=model_id, contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
                reports = json.loads(response.text)
                for gid in results: results[gid]['report'] = reports.get(gid, "報告解析中...")
            except Exception as e:
                for gid in results: results[gid]['report'] = f"AI 分析暫時不可用: {str(e)}"
    return results

# --- 6. 頁面渲染與封存機制 ---
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        d_key = dates[i].strftime('%Y-%m-%d')
        snap_path = f"nba_snapshot_{d_key}.json"
        raw_games = fetch_games_safe(dates[i].strftime('%m/%d/%Y'))
        
        if not raw_games and not os.path.exists(snap_path):
            st.info(f"📅 日期 {d_key} 目前無賽程數據。")
            continue

        if os.path.exists(snap_path):
            with open(snap_path, 'r', encoding='utf-8') as f: data_set = json.load(f)
            st.success("✅ 已載入封存數據")
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
            c3.metric("預測贏家", TEAM_NAME_CH.get(curr_g['win_abbr'], curr_g['win_abbr']), delta=f"領先 {curr_g['diff']} 分")
            
            st.info(curr_g.get('report', '暫無 AI 分析'))
            
            col_l, col_r = st.columns(2)
            with col_l:
                st.write(f"**{curr_g['h_name']} 核心**")
                st.dataframe(pd.DataFrame(curr_g['h_roster']), hide_index=True)
            with col_r:
                st.write(f"**{curr_g['a_name']} 核心**")
                st.dataframe(pd.DataFrame(curr_g['a_roster']), hide_index=True)
