import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2, scoreboardv3, commonteamroster, leaguedashplayerstats
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import os, json, warnings, pytz
from datetime import datetime, timedelta
from google import genai
from google.genai import types

# --- 1. AI 核心設定 (保持每次更動) ---
@st.cache_resource
def init_ai_v7():
    if "GEMINI_API_KEY" in st.secrets:
        try:
            client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
            return client, "gemini-2.0-flash"
        except Exception as e:
            return None, str(e)
    return None, "No API Key"

client_ai, model_id = init_ai_v7()
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

st.set_page_config(page_title="NBA AI 數據專家 v7.0", layout="wide")
st.title("🏀 NBA 終極智慧預測系統 v7.0")

# --- 2. 基礎數據 (強化錯誤攔截) ---
@st.cache_data(ttl=600)
def load_base_data(season):
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
    st.error(f"⚠️ 基礎數據庫加載失敗: {error_msg}")
    st.stop()

# --- 3. 賽程獲取 (修正日期格式，確保不回傳空值) ---
@st.cache_data(ttl=600)
def fetch_games_safe(date_obj):
    # 修正：V3 需要 YYYY-MM-DD
    d_v3 = date_obj.strftime('%Y-%m-%d')
    # 修正：V2 需要 MM/DD/YYYY
    d_v2 = date_obj.strftime('%m/%d/%Y')
    
    try:
        sb3 = scoreboardv3.ScoreboardV3(game_date=d_v3, timeout=30)
        df = sb3.get_data_frames()[0]
        if not df.empty:
            return df.rename(columns={'gameId': 'GAME_ID', 'homeTeamId': 'HOME_TEAM_ID', 'awayTeamId': 'VISITOR_TEAM_ID'}).to_dict('records')
    except: pass
    
    try:
        sb2 = scoreboardv2.ScoreboardV2(game_date=d_v2, timeout=30)
        df = sb2.get_data_frames()[0]
        if not df.empty: return df.to_dict('records')
    except: pass
    
    return []

# --- 4. 核心功能 (保持每次更動：表格精簡) ---
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

# --- 5. 同步分析與 AI (保持每次更動：整數分差) ---
def perform_analysis(raw_list, clf, reg, gf, ps, feats):
    results = {}; ai_input = {}
    t_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
    
    for g in raw_list:
        g_id = str(g.get('GAME_ID', ''))
        h_id, a_id = g.get('HOME_TEAM_ID'), g.get('VISITOR_TEAM_ID')
        if not g_id or not h_id or not a_id: continue
        
        h_code, a_code = t_map.get(h_id), t_map.get(a_id)
        h_f = gf[gf['TEAM_ABBREVIATION'] == h_code].tail(1)
        a_f = gf[gf['TEAM_ABBREVIATION'] == a_code].tail(1)
        if h_f.empty or a_f.empty: continue

        # 預測勝率與整數分差 (保持每次更動)
        h_p = (clf.predict_proba(h_f[feats])[:,1][0] / (clf.predict_proba(h_f[feats])[:,1][0] + clf.predict_proba(a_f[feats])[:,1][0])) * 100
        diff_abs = max(1, round(abs(float(reg.predict(h_f[feats])[0]) - float(reg.predict(a_f[feats])[0]))))
        win_abbr = h_code if h_p > 50 else a_code

        ai_input[g_id] = {'home': TEAM_NAME_CH.get(h_code, h_code), 'away': TEAM_NAME_CH.get(a_code, a_code),
                          'winner': TEAM_NAME_CH.get(win_abbr), 'diff': diff_abs}
        
        results[g_id] = {
            'h_prob': h_p, 'a_prob': 100-h_p, 'diff': diff_abs, 'win_abbr': win_abbr,
            'h_name': TEAM_NAME_CH.get(h_code, h_code), 'a_name': TEAM_NAME_CH.get(a_code, a_code),
            'h_roster': get_roster_stats(h_id, ps), 'a_roster': get_roster_stats(a_id, ps)
        }

    if ai_input and client_ai:
        with st.spinner("🔍 AI 深度分析進行中..."):
            prompt = f"你是 NBA 專家。請為以下比賽寫 180 字以上分析，包含整數分差與勝率。回傳 JSON: {{'場次ID': '內容'}}。\n{ai_input}"
            try:
                res = client_ai.models.generate_content(model=model_id, contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json"))
                reports = json.loads(res.text)
                for gid in results: results[gid]['report'] = reports.get(gid, "分析已生成")
            except:
                for gid in results: results[gid]['report'] = "AI 繁忙，請手動鎖定數據後查看。"
                
    return results

# --- 6. 介面渲染 (修復截圖中的空白問題) ---
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        d_obj = dates[i]
        d_key = d_obj.strftime('%Y-%m-%d')
        snap_path = f"nba_snapshot_{d_key}.json"
        
        # 1. 優先嘗試讀取封存
        if os.path.exists(snap_path):
            with open(snap_path, 'r', encoding='utf-8') as f: data_set = json.load(f)
            st.success(f"📦 已讀取 {d_key} 封存數據")
        else:
            # 2. 如果沒封存，則抓取即時數據
            raw_games = fetch_games_safe(d_obj)
            if not raw_games:
                st.info(f"🚫 NBA 官網目前尚未更新 {d_key} 的賽程數據。")
                data_set = {}
            else:
                data_set = perform_analysis(raw_games, clf, reg, gf, ps, feats)
                if data_set:
                    if st.button("🔒 鎖定今日數據並儲存", key=f"btn_{d_key}"):
                        with open(snap_path, 'w', encoding='utf-8') as f:
                            json.dump(data_set, f, ensure_ascii=False)
                        st.rerun()

        # 3. 渲染遊戲清單 (確保有 data_set 才渲染)
        if data_set:
            game_list = [f"{v['a_name']} @ {v['h_name']}" for v in data_set.values()]
            sel_game = st.selectbox("🎯 選擇場次進行分析", game_list, key=f"sel_{d_key}")
            
            curr_g = next(v for v in data_set.values() if f"{v['a_name']} @ {v['h_name']}" == sel_game)
            
            st.markdown(f"### 🏟️ {sel_game}")
            c1, c2, c3 = st.columns(3)
            c1.metric(f"🏠 {curr_g['h_name']}", f"{curr_g['h_prob']:.1f}%")
            c2.metric(f"✈️ {curr_g['a_name']}", f"{curr_g['a_prob']:.1f}%")
            c3.metric("預測贏家", TEAM_NAME_CH.get(curr_g['win_abbr'], curr_g['win_abbr']), delta=f"領先 {curr_g['diff']} 分")
            
            st.info(curr_g.get('report', '暫無報告'))
            
            l_col, r_col = st.columns(2)
            with l_col:
                st.write(f"**{curr_g['h_name']} 核心**")
                st.dataframe(pd.DataFrame(curr_g['h_roster']), hide_index=True)
            with r_col:
                st.write(f"**{curr_g['a_name']} 核心**")
                st.dataframe(pd.DataFrame(curr_g['a_roster']), hide_index=True)
        else:
            st.write("等待數據更新中...")
