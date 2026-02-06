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

st.set_page_config(page_title="NBA 2026 終極封盤預測系統", layout="wide")
st.title("🏀 NBA 終極預測系統 (凌晨 12 點鎖定版)")

# --- 封盤邏輯路徑 ---
def get_snapshot_path(date_key):
    return f"nba_snapshot_{date_key}.json"

# --- 1. 數據與模型 (強化版) ---
@st.cache_data(ttl=600)
def get_comprehensive_data(season):
    gamefinder = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60)
    all_games = gamefinder.get_data_frames()[0]
    all_games['GAME_DATE'] = pd.to_datetime(all_games['GAME_DATE'])
    all_games = all_games.sort_values(['TEAM_ID', 'GAME_DATE'])

    # 特徵工程
    all_games['IS_HOME'] = all_games['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    all_games['WIN_BIN'] = all_games['WL'].apply(lambda x: 1 if x == 'W' else 0)
    all_games['L3_WIN_RATE'] = all_games.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(3).mean())
    all_games['L10_WIN_RATE'] = all_games.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10).mean())
    
    stats_cols = ['PTS', 'PLUS_MINUS', 'FG_PCT', 'TOV']
    for col in stats_cols:
        all_games[f'L5_{col}'] = all_games.groupby('TEAM_ID')[col].transform(lambda x: x.shift(1).rolling(5).mean())

    all_games['DAYS_REST'] = all_games.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days
    all_games['B2B'] = (all_games['DAYS_REST'] == 1).astype(int)
    
    train_df = all_games.dropna(subset=['L5_PTS', 'L10_WIN_RATE']).copy()
    features = [f'L5_{c}' for c in stats_cols] + ['B2B', 'IS_HOME', 'L3_WIN_RATE', 'L10_WIN_RATE']
    model = xgb.XGBClassifier(n_estimators=150, max_depth=4, learning_rate=0.05, eval_metric='logloss')
    model.fit(train_df[features], train_df['WIN_BIN'])

    p_stats = leaguedashplayerstats.LeagueDashPlayerStats(season=season, per_mode_detailed='PerGame').get_data_frames()[0]
    for col in ['PTS', 'REB', 'AST', 'STL']:
        p_stats[col] = pd.to_numeric(p_stats[col], errors='coerce').fillna(0)
    player_stats = p_stats[['PLAYER_ID', 'PLAYER_NAME', 'TEAM_ID', 'PTS', 'REB', 'AST', 'STL']]

    return model, all_games, player_stats, features

@st.cache_data(ttl=600)
def get_team_roster(team_id):
    try:
        roster = commonteamroster.CommonTeamRoster(team_id=team_id).get_data_frames()[0]
        return roster[['PLAYER_ID', 'PLAYER']]
    except: return pd.DataFrame(columns=['PLAYER_ID', 'PLAYER'])

@st.cache_data(ttl=3600)
def get_schedule_for_date(date_obj):
    date_str = date_obj.strftime('%m/%d/%Y')
    try:
        sb = scoreboardv2.ScoreboardV2(game_date=date_str, timeout=30)
        df = sb.get_data_frames()[0]
        all_teams_list = teams.get_teams()
        t_map = {t['id']: t['abbreviation'] for t in all_teams_list}
        if not df.empty:
            df['HOME_ABBR'] = df['HOME_TEAM_ID'].map(t_map)
            df['AWAY_ABBR'] = df['VISITOR_TEAM_ID'].map(t_map)
            return df.to_dict('records')
    except: pass
    return []

# --- 2. 核心：處理單日的封盤預測 ---
def get_locked_results_for_date(date_key, games, model, all_games_raw, player_stats, features_list):
    snapshot_file = get_snapshot_path(date_key)
    now_tw = datetime.now(tw_tz)
    
    # 如果已有鎖定檔，直接讀取
    if os.path.exists(snapshot_file):
        with open(snapshot_file, 'r', encoding='utf-8') as f:
            return json.load(f), True

    # 計算預測
    results = {}
    for g in games:
        h_abbr, a_abbr = g['HOME_ABBR'], g['AWAY_ABBR']
        h_id, a_id = g['HOME_TEAM_ID'], g['VISITOR_TEAM_ID']
        
        h_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == h_abbr].tail(1)[features_list].copy()
        a_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == a_abbr].tail(1)[features_list].copy()
        h_feat['IS_HOME'] = 1
        a_feat['IS_HOME'] = 0
        
        h_p = float(model.predict_proba(h_feat)[:, 1])
        a_p = float(model.predict_proba(a_feat)[:, 1])
        h_prob, a_prob = (h_p/(h_p+a_p))*100, (a_p/(h_p+a_p))*100
        
        def get_clean_roster(t_id):
            ros = get_team_roster(t_id)
            m = ros.merge(player_stats, on='PLAYER_ID', how='left')
            c = m[~((m['TEAM_ID'] != t_id) & (m['TEAM_ID'] != 0) & (m['TEAM_ID'].notnull()))]
            return c.sort_values(by='PTS', ascending=False).head(5).to_dict('records')

        results[str(g['GAME_ID'])] = {
            'home_prob': h_prob, 'away_prob': a_prob,
            'home_roster': get_clean_roster(h_id),
            'away_roster': get_clean_roster(a_id),
            'lock_time': now_tw.strftime('%Y-%m-%d %H:%M:%S')
        }
    
    # 若是「今天」且過了 00:00，則存檔
    if date_key == now_tw.strftime('%Y-%m-%d') and now_tw.hour >= 0:
        with open(snapshot_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False)
            
    return results, False

# --- 3. 啟動與 UI 標籤頁 ---
with st.spinner('🔄 正在同步 NBA 數據與模型...'):
    model, all_games_raw, all_player_stats, features_list = get_comprehensive_data('2025-26')

# 建立前三天到今天的日期列表
date_list = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in date_list])

for i, tab in enumerate(tabs):
    with tab:
        current_date = date_list[i]
        date_key = current_date.strftime('%Y-%m-%d')
        
        games = get_schedule_for_date(current_date)
        
        if not games:
            st.info(f"📅 {date_key} 暫無賽程資訊")
        else:
            locked_data, is_locked = get_locked_results_for_date(
                date_key, games, model, all_games_raw, all_player_stats, features_list
            )
            
            options = [f"{TEAM_NAME_CH.get(g['AWAY_ABBR'], g['AWAY_ABBR'])} @ {TEAM_NAME_CH.get(g['HOME_ABBR'], g['HOME_ABBR'])}" for g in games]
            sel_game_idx = st.selectbox("🎯 選擇場次", range(len(options)), format_func=lambda x: options[x], key=f"select_{date_key}")
            
            g_data = games[sel_game_idx]
            res = locked_data.get(str(g_data['GAME_ID']))
            
            if res:
                if is_locked: st.caption(f"🔒 封盤數據 (鎖定時間: {res['lock_time']})")
                
                c1, c2 = st.columns(2)
                c1.metric(f"{TEAM_NAME_CH.get(g_data['HOME_ABBR'])} 勝率", f"{res['home_prob']:.1f}%")
                c2.metric(f"{TEAM_NAME_CH.get(g_data['AWAY_ABBR'])} 勝率", f"{res['away_prob']:.1f}%")
                
                # H2H 對戰紀錄
                st.write("#### ⚔️ 本季對戰紀錄 (H2H)")
                h_id, a_abbr = g_data['HOME_TEAM_ID'], g_data['AWAY_ABBR']
                h2h = all_games_raw[((all_games_raw['TEAM_ID'] == h_id) & (all_games_raw['MATCHUP'].str.contains(a_abbr)))]
                if not h2h.empty:
                    h2h_df = h2h[['GAME_DATE', 'MATCHUP', 'WL', 'PTS']].copy()
                    h2h_df['GAME_DATE'] = h2h_df['GAME_DATE'].dt.strftime('%Y-%m-%d')
                    st.table(h2h_df.head(5))
                else: st.caption("本季尚未交手")

                # 名單
                st.write("#### 👤 核心球員 (封盤狀態)")
                ch, ca = st.columns(2)
                with ch:
                    st.caption(TEAM_NAME_CH.get(g_data['HOME_ABBR']))
                    st.dataframe(pd.DataFrame(res['home_roster'])[['PLAYER_NAME', 'PTS', 'REB', 'AST']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分'}), hide_index=True)
                with ca:
                    st.caption(TEAM_NAME_CH.get(g_data['AWAY_ABBR']))
                    st.dataframe(pd.DataFrame(res['away_roster'])[['PLAYER_NAME', 'PTS', 'REB', 'AST']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分'}), hide_index=True)
                
                st.success(f"📌 系統推薦：{TEAM_NAME_CH.get(g_data['HOME_ABBR'] if res['home_prob'] > res['away_prob'] else g_data['AWAY_ABBR'])}")
