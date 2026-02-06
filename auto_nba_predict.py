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

# --- 1. AI 核心設定 (從 Secrets 讀取) ---
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

st.set_page_config(page_title="NBA AI 智慧預測 v5.2", layout="wide")
st.title("🏀 NBA 終極智慧預測系統")

# --- 2. API 診斷小工具 ---
with st.sidebar:
    st.header("🛠️ 系統狀態")
    if AI_READY:
        st.success("Gemini API: 已連線")
    else:
        st.error("Gemini API: 未設定 (請檢查 Secrets)")
    st.info(f"更新時間: {datetime.now(tw_tz).strftime('%Y-%m-%d %H:%M')}")

# --- 3. 功能函數 ---
def get_snapshot_path(date_key):
    return f"nba_snapshot_{date_key}.json"

def generate_ai_report(h_n, a_n, h_stats, a_stats, winner, diff):
    """呼叫 API 生成分析，若失敗則回傳保險文字"""
    if not AI_READY:
        return f"【系統提示】API Key 未設定，模型預測 {winner} 勝出，分差約 {round(abs(diff), 1)} 分。"

    clean_diff = round(abs(float(diff)), 1)
    prompt = f"""
    你是一位 NBA 專業分析師。請針對以下比賽數據寫一段深入的賽前短評：
    【對戰】{a_n} (客) @ {h_n} (主)
    【主隊】勝率 {h_stats['wr']:.0f}%，近5場均得分 {h_stats['pts']:.1f}，B2B：{h_stats['b2b']}
    【客隊】勝率 {a_stats['wr']:.0f}%，近5場均得分 {a_stats['pts']:.1f}，B2B：{a_stats['b2b']}
    【模型預測】看好 {winner} 贏 {clean_diff} 分。
    請撰寫約 150 字繁體中文分析，說明勝負關鍵點與預測邏輯。
    """
    try:
        response = model_ai.generate_content(prompt)
        if response and response.text:
            return response.text
        return f"根據數據，{winner} 近期火力強大（場均 {max(h_stats['pts'], a_stats['pts']):.1f}），看好其能控制比賽節奏並取得勝利。"
    except Exception as e:
        return f"【數據快評】{winner} 在近期勝率與對位上擁有優勢，模型給出 {clean_diff} 分的預期分差。建議關注其開局進攻效率。"

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

# --- 4. 預測引擎 ---
def run_prediction(games, clf, reg, all_games_raw, player_stats, features_list):
    results = {}
    for g in games:
        h_abbr, a_abbr = g['HOME_ABBR'], g['AWAY_ABBR']
        h_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == h_abbr].tail(1)
        a_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == a_abbr].tail(1)
        
        if h_feat.empty or a_feat.empty: continue

        h_in = h_feat[features_list].copy(); h_in['IS_HOME'] = 1
        a_in = a_feat[features_list].copy(); a_in['IS_HOME'] = 0
        
        h_p = (float(clf.predict_proba(h_in)[:, 1][0]) / (float(clf.predict_proba(h_in)[:, 1][0]) + float(clf.predict_proba(a_in)[:, 1][0]))) * 100
        diff = float(reg.predict(h_in)[0]) - float(reg.predict(a_in)[0])
        
        h_data = {'wr': h_feat['L10_WIN_RATE'].values[0]*100, 'pts': h_feat['L5_PTS'].values[0], 'b2b': '是' if h_feat['B2B'].values[0] else '否'}
        a_data = {'wr': a_feat['L10_WIN_RATE'].values[0]*100, 'pts': a_feat['L5_PTS'].values[0], 'b2b': '是' if a_feat['B2B'].values[0] else '否'}
        
        h_n_ch, a_n_ch = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
        winner_n = h_n_ch if diff > 0 else a_n_ch
        
        ai_report = generate_ai_report(h_n_ch, a_n_ch, h_data, a_data, winner_n, diff)

        def get_roster_data(t_id):
            ros = get_team_roster(t_id)
            if ros.empty or player_stats.empty: return []
            m = ros.merge(player_stats, on='PLAYER_NAME', how='left').fillna(0)
            return m.sort_values(by='PTS', ascending=False).head(5).to_dict('records')

        results[str(g['GAME_ID'])] = {
            'h_prob': float(h_p), 'a_prob': float(100 - h_p), 'diff': float(round(diff, 1)),
            'winner_abbr': h_abbr if diff > 0 else a_abbr,
            'h_idx': [f"🟢 勝率: {h_data['wr']:.0f}%", f"🟢 均得分: {h_data['pts']:.1f}", f"🔴 B2B: {h_data['b2b']}"],
            'a_idx': [f"🔵 勝率: {a_data['wr']:.0f}%", f"🔵 均得分: {a_data['pts']:.1f}", f"🔴 B2B: {a_data['b2b']}"],
            'summary_report': ai_report,
            'h_roster': get_roster_data(g['HOME_TEAM_ID']), 'a_roster': get_roster_data(g['VISITOR_TEAM_ID'])
        }
    return results

# --- 5. 介面主體 ---
clf, reg, all_games_raw, player_stats, features = get_comprehensive_data('2025-26')
date_list = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in date_list])

for i, tab in enumerate(tabs):
    with tab:
        current_date = date_list[i]; date_key = current_date.strftime('%Y-%m-%d')
        games = get_schedule_for_date(current_date); snapshot_file = get_snapshot_path(date_key)
        if not games: st.info("暫無賽程"); continue

        is_locked = os.path.exists(snapshot_file)
        c_btn, c_txt = st.columns([1, 4])
        if not is_locked:
            if c_btn.button("🔒 鎖定數據", key=f"lk_{date_key}"):
                with st.spinner("AI 正在分析場次中..."):
                    ld = run_prediction(games, clf, reg, all_games_raw, player_stats, features)
                    with open(snapshot_file, 'w', encoding='utf-8') as f: json.dump(ld, f, ensure_ascii=False)
                st.rerun()
            c_txt.warning("⚠️ 即時模式：數據隨 API 變動")
        else:
            if c_btn.button("🔓 解鎖更新", key=f"ul_{date_key}"):
                os.remove(snapshot_file); st.rerun()
            c_txt.success("🔒 封盤模式：顯示已存檔分析")

        game_names = [f"{TEAM_NAME_CH.get(g['AWAY_ABBR'], g['AWAY_ABBR'])} @ {TEAM_NAME_CH.get(g['HOME_ABBR'], g['HOME_ABBR'])}" for g in games]
        sel_name = st.selectbox("🎯 選擇對戰場次", options=game_names, key=f"sb_{date_key}")
        
        if is_locked:
            with open(snapshot_file, 'r', encoding='utf-8') as f: ds = json.load(f)
        else:
            ds = run_prediction(games, clf, reg, all_games_raw, player_stats, features)

        g_id = str(games[game_names.index(sel_name)]['GAME_ID'])
        res = ds.get(g_id, {})
        
        if res:
            h_n, a_n = TEAM_NAME_CH.get(games[game_names.index(sel_name)]['HOME_ABBR']), TEAM_NAME_CH.get(games[game_names.index(sel_name)]['AWAY_ABBR'])
            st.markdown(f"## 🏟️ {a_n} @ {h_n}")
            
            c1, c2, c3 = st.columns(3)
            c1.metric(f"{h_n} 勝率", f"{float(res['h_prob']):.1f}%")
            c2.metric(f"{a_n} 勝率", f"{float(res['a_prob']):.1f}%")
            c3.metric("預測贏家", TEAM_NAME_CH.get(res['winner_abbr']), delta=f"領先 {abs(res['diff'])} 分")

            st.write("---")
            st.subheader("📝 AI 深度客製化分析報告")
            st.info(res.get('summary_report', "數據分析中..."))

            l_col, r_col = st.columns(2)
            with l_col:
                st.markdown(f"**🏠 {h_n} 指標**")
                for item in res.get('h_idx', []): st.write(item)
            with r_col:
                st.markdown(f"**✈️ {a_n} 指標**")
                for item in res.get('a_idx', []): st.write(item)

            st.write("---")
            st.subheader("👤 核心球員場均數據")
            def safe_df(data):
                df = pd.DataFrame(data if data else [])
                return df[['PLAYER_NAME','PTS','REB','AST']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分','REB':'籃板','AST':'助攻'}) if not df.empty else pd.DataFrame(columns=['姓名','得分','籃板','助攻'])
            cl, cr = st.columns(2)
            cl.dataframe(safe_df(res.get('h_roster')), hide_index=True, use_container_width=True)
            cr.dataframe(safe_df(res.get('a_roster')), hide_index=True, use_container_width=True)
