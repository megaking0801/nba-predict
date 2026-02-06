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

# --- 基礎設定 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')

TEAM_NAME_CH = {
    'ATL': '亞特蘭大老鷹', 'BKN': '布魯克林籃網', 'BOS': '波士頓塞爾提克',
    'CHA': '夏洛特黃蜂', 'CHI': '芝加哥公牛', 'CLE': '克里夫蘭騎士',
    'DAL': '達拉斯獨行俠', 'DEN': '丹佛金塊', 'DET': '底特律活塞',
    'GSW': '金州勇士', 'HOU': '休士頓火箭', 'IND': '印第安納溜馬',
    'LAC': '洛杉磯快艇', 'LAL': '洛杉磯湖人', 'MEM': '曼非斯灰熊',
    'MIA': '邁阿密熱火', 'MIL': '密爾瓦基公鹿', 'MIN': '明尼蘇達狼',
    'NOP': '紐奧良鵜鶘', 'NYK': '紐約尼克', 'OKC': '奧克拉荷馬雷霆',
    'ORL': '奧蘭多魔術', 'PHI': '費城 76 人', 'PHX': '鳳凰城太陽',
    'POR': '波特蘭開拓者', 'SAC': '沙加謬度國王', 'SAS': '聖安東尼奧馬刺',
    'TOR': '多倫多暴龍', 'UTA': '猶他爵士', 'WAS': '華盛頓巫師'
}

st.set_page_config(page_title="NBA 2026 深度解析系統 v4.0", layout="wide")
st.title("🏀 NBA 終極預測系統 (雙方優缺點詳盡分析版)")

def get_snapshot_path(date_key):
    return f"nba_snapshot_{date_key}.json"

# --- 1. 數據與模型 ---
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

# --- 2. 深度雙向分析引擎 ---
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
        
        # 準備雙方優缺點分析
        analysis = {"home": [], "away": []}
        
        # 主隊分析
        h_win_rate = h_feat['L10_WIN_RATE'].values[0] * 100
        h_pts = h_feat['L5_PTS'].values[0]
        analysis["home"].append(f"🟢 近十場勝率: {h_win_rate:.0f}%")
        analysis["home"].append(f"🟢 近五場均得分: {h_pts:.1f}")
        if h_feat['B2B'].values[0] == 1: analysis["home"].append("🔴 警訊: 背靠背作戰，體能堪憂")
        else: analysis["home"].append("🟢 體能充足: 非連戰狀態")

        # 客隊分析
        a_win_rate = a_feat['L10_WIN_RATE'].values[0] * 100
        a_pts = a_feat['L5_PTS'].values[0]
        analysis["away"].append(f"🔵 近十場勝率: {a_win_rate:.0f}%")
        analysis["away"].append(f"🔵 近五場均得分: {a_pts:.1f}")
        if a_feat['B2B'].values[0] == 1: analysis["away"].append("🔴 警訊: 背靠背作戰，體能堪憂")
        else: analysis["away"].append("🔵 體能充足: 非連戰狀態")

        results[str(g['GAME_ID'])] = {
            'h_prob': h_p, 'a_prob': 100 - h_p,
            'diff': round(diff, 1),
            'winner_abbr': h_abbr if diff > 0 else a_abbr,
            'home_analysis': analysis["home"],
            'away_analysis': analysis["away"],
            'lock_time': datetime.now(tw_tz).strftime('%H:%M:%S')
        }
    return results

# --- 3. UI 渲染 ---
clf, reg, all_games_raw, player_stats, features = get_comprehensive_data('2025-26')

date_list = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in date_list])

for i, tab in enumerate(tabs):
    with tab:
        current_date = date_list[i]; date_key = current_date.strftime('%Y-%m-%d')
        games = get_schedule_for_date(current_date); snapshot_file = get_snapshot_path(date_key)
        
        if not games: st.info("暫無賽程"); continue

        is_locked = os.path.exists(snapshot_file)
        if is_locked:
            with open(snapshot_file, 'r', encoding='utf-8') as f: locked_data = json.load(f)
            st.success(f"🔒 數據已封盤")
        else:
            locked_data = run_prediction(games, clf, reg, all_games_raw, player_stats, features)
            st.warning("⏳ 即時預測模式")

        options = [f"{TEAM_NAME_CH.get(g['AWAY_ABBR'])} @ {TEAM_NAME_CH.get(g['HOME_ABBR'])}" for g in games]
        sel_idx = st.selectbox("🎯 選擇場次", range(len(options)), key=f"s_{date_key}")
        
        g_data = games[sel_idx]
        res = locked_data.get(str(g_data['GAME_ID']), {})
        
        if res:
            h_n, a_n = TEAM_NAME_CH.get(g_data['HOME_ABBR']), TEAM_NAME_CH.get(g_data['AWAY_ABBR'])
            w_n = TEAM_NAME_CH.get(res.get('winner_abbr'))
            
            # 頂部儀表板
            c1, c2, c3 = st.columns(3)
            c1.metric(f"{h_n} 勝率", f"{res.get('h_prob', 0):.1f}%")
            c2.metric(f"{a_n} 勝率", f"{res.get('a_prob', 0):.1f}%")
            c3.metric("預測贏家", w_n, delta=f"預計贏 {abs(res.get('diff', 0))} 分")

            st.write("---")
            # 雙方優缺點深度對比
            st.subheader("🕵️ 深度戰力解析")
            left, right = st.columns(2)
            with left:
                st.markdown(f"#### 🏠 {h_n}")
                for item in res.get('home_analysis', []): st.write(item)
            with right:
                st.markdown(f"#### ✈️ {a_n}")
                for item in res.get('away_analysis', []): st.write(item)

            st.write("---")
            # 綜合建議區
            st.markdown(f"### 🎯 總結建議")
            diff_abs = abs(res.get('diff', 0))
            if diff_abs > 8:
                st.success(f"🔥 **強烈推薦：{w_n}**。雙方戰力落差顯著，建議直接鎖定讓分盤。")
            elif diff_abs > 3:
                st.info(f"✅ **穩定推薦：{w_n}**。預測有一定容錯空間，穩定性高。")
            else:
                st.warning(f"⚠️ **保守觀望**：雙方分差僅 {diff_abs} 分。實力極其接近，不建議重注讓分盤。")

            # 鎖定/解鎖按鈕
            if not is_locked:
                if st.button("🔒 鎖定今日數據", key=f"lk_{date_key}"):
                    with open(snapshot_file, 'w', encoding='utf-8') as f: json.dump(locked_data, f, ensure_ascii=False)
                    st.rerun()
            else:
                if st.button("🔓 解鎖數據", key=f"ul_{date_key}"):
                    os.remove(snapshot_file); st.rerun()
