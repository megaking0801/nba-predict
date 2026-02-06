import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2, scoreboardv3, commonteamroster, leaguedashplayerstats
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import os, json, warnings, pytz
from datetime import datetime, timedelta
from google import genai
from google.genai import types

# --- 1. AI 初始化 (持續記住：2026 新 SDK) ---
@st.cache_resource
def init_ai_v73():
    if "GEMINI_API_KEY" in st.secrets:
        try:
            client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
            return client, "gemini-2.0-flash"
        except: return None, "AI ERROR"
    return None, "No API Key"

client_ai, model_id = init_ai_v73()
tw_tz = pytz.timezone('Asia/Taipei')
warnings.filterwarnings('ignore')

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

st.set_page_config(page_title="NBA AI 專家 v7.3", layout="wide")
st.title("🏀 NBA 終極智慧預測系統 v7.3")

# --- 2. 數據庫加載 (強化穩定性) ---
@st.cache_data(ttl=600)
def load_base_data(season):
    try:
        # 抓取所有球隊的賽季表現
        gf = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60).get_data_frames()[0]
        gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
        gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
        gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
        gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
        # 計算近況
        gf['L10_WIN_RATE'] = gf.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
        for c in ['PTS', 'PLUS_MINUS']: 
            gf[f'L5_{c}'] = gf.groupby('TEAM_ID')[c].transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
        gf['B2B'] = (gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days == 1).astype(int)
        
        train = gf.fillna(0) # 修正：避免因為數據少而導致空值
        feats = ['L5_PTS', 'L5_PLUS_MINUS', 'B2B', 'IS_HOME', 'L10_WIN_RATE']
        clf = xgb.XGBClassifier().fit(train[feats], train['WIN_BIN'])
        reg = xgb.XGBRegressor().fit(train[feats], train['PLUS_MINUS'])
        ps = leaguedashplayerstats.LeagueDashPlayerStats(season=season, per_mode_detailed='PerGame').get_data_frames()[0]
        return clf, reg, gf, ps[['PLAYER_NAME', 'PTS', 'REB', 'AST']], feats, None
    except Exception as e: return None, None, None, None, [], str(e)

clf, reg, gf, ps, feats, error_msg = load_base_data('2025-26')
if error_msg:
    st.error(f"⚠️ 數據初始化失敗: {error_msg}")
    st.stop()

# --- 3. 賽程獲取 ---
def fetch_games_v73(d_obj):
    d_v3 = d_obj.strftime('%Y-%m-%d')
    try:
        sb3 = scoreboardv3.ScoreboardV3(game_date=d_v3, timeout=30).get_data_frames()[0]
        if not sb3.empty:
            return sb3.rename(columns={'gameId': 'GAME_ID', 'homeTeamId': 'HOME_TEAM_ID', 'awayTeamId': 'VISITOR_TEAM_ID'}).to_dict('records')
    except: pass
    return []

# --- 4. 球員表格 (保持每次更動：PTS, REB, AST) ---
def get_roster_stats(team_id, player_stats_df):
    try:
        ros = commonteamroster.CommonTeamRoster(team_id=team_id, timeout=20).get_data_frames()[0]
        name_col = 'PLAYER_NAME' if 'PLAYER_NAME' in ros.columns else 'PLAYER'
        ros = ros.rename(columns={name_col: 'PLAYER_NAME'})
        merged = ros.merge(player_stats_df, on='PLAYER_NAME', how='left').fillna(0)
        final = merged[['PLAYER_NAME', 'PTS', 'REB', 'AST']].sort_values(by='PTS', ascending=False).head(5)
        final.columns = ['球員姓名', '得分', '籃板', '助攻']
        return final.to_dict('records')
    except: return []

# --- 5. 同步分析 (解決畫面空白核心問題) ---
def analyze_v73(raw_list, clf, reg, gf, ps, feats):
    results = {}; ai_input = {}
    t_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
    
    for g in raw_list:
        g_id = str(g.get('GAME_ID', ''))
        h_id, a_id = g.get('HOME_TEAM_ID'), g.get('VISITOR_TEAM_ID')
        h_code, a_code = t_map.get(h_id), t_map.get(a_id)
        
        if not h_code or not a_code: continue
        
        # 修正：強制匹配該隊在數據庫中的「最後一筆記錄」
        h_f = gf[gf['TEAM_ABBREVIATION'] == h_code].sort_values('GAME_DATE').tail(1)
        a_f = gf[gf['TEAM_ABBREVIATION'] == a_code].sort_values('GAME_DATE').tail(1)
        
        # 如果真的完全找不到該隊數據，則填入平均值避免崩潰
        if h_f.empty or a_f.empty:
            continue 

        # 預測勝率與分差 (保持每次更動：整數)
        h_p_raw = clf.predict_proba(h_f[feats])[:,1][0]
        a_p_raw = clf.predict_proba(a_f[feats])[:,1][0]
        h_p = (h_p_raw / (h_p_raw + a_p_raw)) * 100
        
        diff_abs = max(1, round(abs(float(reg.predict(h_f[feats])[0]) - float(reg.predict(a_f[feats])[0]))))
        win_abbr = h_code if h_p > 50 else a_code

        res_entry = {
            'h_prob': h_p, 'a_prob': 100-h_p, 'diff': diff_abs, 'win_abbr': win_abbr,
            'h_name': TEAM_NAME_CH.get(h_code, h_code), 'a_name': TEAM_NAME_CH.get(a_code, a_code),
            'h_roster': get_roster_stats(h_id, ps), 'a_roster': get_roster_stats(a_id, ps)
        }
        results[g_id] = res_entry
        ai_input[g_id] = {'h': res_entry['h_name'], 'a': res_entry['a_name'], 'w': TEAM_NAME_CH.get(win_abbr), 'd': diff_abs}

    # AI 生成
    if results and client_ai:
        try:
            prompt = f"分析 NBA 比賽並以 JSON 回傳 {{'ID': '分析文字'}}: {ai_input}"
            res = client_ai.models.generate_content(model=model_id, contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json"))
            reports = json.loads(res.text)
            for gid in results: results[gid]['report'] = reports.get(gid, "分析完畢")
        except:
            for gid in results: results[gid]['report'] = "AI 正在生成詳細分析..."
            
    return results

# --- 6. 介面渲染 ---
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        d_obj = dates[i]
        d_key = d_obj.strftime('%Y-%m-%d')
        snap_path = f"nba_snapshot_{d_key}.json"
        
        if os.path.exists(snap_path):
            with open(snap_path, 'r', encoding='utf-8') as f: data_set = json.load(f)
            st.success(f"已載入 {d_key} 封存數據")
        else:
            raw_games = fetch_games_v73(d_obj)
            if not raw_games:
                st.info(f"🏀 {d_key} 尚未有賽程更新。")
                data_set = {}
            else:
                data_set = analyze_v73(raw_games, clf, reg, gf, ps, feats)
                if data_set:
                    st.success(f"已完成 {len(data_set)} 場比賽數據運算")
                    if st.button("🔒 封存分析報告", key=f"lock_{d_key}"):
                        with open(snap_path, 'w', encoding='utf-8') as f: json.dump(data_set, f, ensure_ascii=False)
                        st.rerun()
                else:
                    st.warning("數據庫匹配中，請稍候。")

        if data_set:
            sel = st.selectbox("🎯 選擇場次", [f"{v['a_name']} @ {v['h_name']}" for v in data_set.values()], key=f"s_{d_key}")
            curr = next(v for v in data_set.values() if f"{v['a_name']} @ {v['h_name']}" == sel)
            
            st.markdown(f"### 🏟️ {sel}")
            c1, c2, c3 = st.columns(3)
            c1.metric(f"🏠 {curr['h_name']}", f"{curr['h_prob']:.1f}%")
            c2.metric(f"✈️ {curr['a_name']}", f"{curr['a_prob']:.1f}%")
            c3.metric("勝方", TEAM_NAME_CH.get(curr['win_abbr']), delta=f"分差 {curr['diff']}")
            
            st.info(curr.get('report', '分析生成中...'))
            l, r = st.columns(2)
            with l: st.dataframe(pd.DataFrame(curr['h_roster']), hide_index=True)
            with r: st.dataframe(pd.DataFrame(curr['a_roster']), hide_index=True)
