import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2, commonteamroster, leaguedashplayerstats
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
from datetime import datetime, timedelta
import warnings

warnings.filterwarnings('ignore')

# 1. 隊伍中英文對照表
TEAM_NAME_CH = {
    'ATL': '亞特蘭大老鷹', 'BKN': '布魯克林籃網', 'BOS': '波士頓塞爾提克',
    'CHA': '夏洛特黃蜂', 'CHI': '芝加哥公牛', 'CLE': '克里夫蘭騎士',
    'DAL': '達拉斯獨行俠', 'DEN': '丹佛金塊', 'DET': '底特律活塞',
    'GSW': '金州勇士', 'HOU': '休士頓火箭', 'IND': '印第安納溜馬',
    'LAC': '洛杉磯快艇', 'LAL': '洛杉磯湖人', 'MEM': '曼非斯灰熊',
    'MIA': '邁阿密熱火', 'MIL': '密爾瓦基公鹿', 'MIN': '明尼蘇達狼',
    'NOP': '紐奧良鵜鶘', 'NYK': '紐約尼克', 'OKC': '奧克拉荷馬雷霆',
    'ORL': '奧蘭多魔術', 'PHI': '費城 76 人', 'PHX': '鳳凰城太陽',
    'POR': '波特蘭開拓者', 'SAC': '沙加緬度國王', 'SAS': '聖安東尼奧馬刺',
    'TOR': '多倫多暴龍', 'UTA': '猶他爵士', 'WAS': '華盛頓巫師'
}

st.set_page_config(page_title="NBA 2026 強化預測系統", layout="wide")
st.title("🏀 NBA 數據預測 (強化版：加入近況與主場加權)")

all_teams = teams.get_teams()
team_map = {team['id']: team['abbreviation'] for team in all_teams}

@st.cache_data(ttl=600)
def get_comprehensive_data(season):
    gamefinder = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60)
    all_games = gamefinder.get_data_frames()[0]
    all_games['GAME_DATE'] = pd.to_datetime(all_games['GAME_DATE'])
    all_games = all_games.sort_values(['TEAM_ID', 'GAME_DATE'])

    # --- 核心優化：特徵工程 2.0 ---
    # 1. 主客場識別 (MATCHUP 含有 'vs.' 代表主場)
    all_games['IS_HOME'] = all_games['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    
    # 2. 計算近期趨勢 (近 3 場與近 10 場)
    all_games['WIN_BIN'] = all_games['WL'].apply(lambda x: 1 if x == 'W' else 0)
    all_games['L3_WIN_RATE'] = all_games.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(3).mean())
    all_games['L10_WIN_RATE'] = all_games.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10).mean())
    
    # 3. 基礎統計數據
    stats_cols = ['PTS', 'PLUS_MINUS', 'FG_PCT', 'TOV']
    for col in stats_cols:
        all_games[f'L5_{col}'] = all_games.groupby('TEAM_ID')[col].transform(lambda x: x.shift(1).rolling(5).mean())

    # 4. 休息天數與 B2B
    all_games['DAYS_REST'] = all_games.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days
    all_games['B2B'] = (all_games['DAYS_REST'] == 1).astype(int)
    
    # 訓練模型
    train_df = all_games.dropna(subset=['L5_PTS', 'L10_WIN_RATE']).copy()
    features = [f'L5_{c}' for c in stats_cols] + ['B2B', 'IS_HOME', 'L3_WIN_RATE', 'L10_WIN_RATE']
    
    model = xgb.XGBClassifier(n_estimators=150, max_depth=4, learning_rate=0.05, eval_metric='logloss')
    model.fit(train_df[features], train_df['WIN_BIN'])

    # 球員數據
    player_stats_raw = leaguedashplayerstats.LeagueDashPlayerStats(season=season, per_mode_detailed='PerGame').get_data_frames()[0]
    for col in ['PTS', 'REB', 'AST', 'STL']:
        player_stats_raw[col] = pd.to_numeric(player_stats_raw[col], errors='coerce').fillna(0)
    player_stats = player_stats_raw[['PLAYER_ID', 'PLAYER_NAME', 'TEAM_ID', 'PTS', 'REB', 'AST', 'STL']]

    return model, all_games, player_stats, features

@st.cache_data(ttl=600)
def get_team_roster(team_id):
    try:
        roster = commonteamroster.CommonTeamRoster(team_id=team_id).get_data_frames()[0]
        return roster[['PLAYER_ID', 'PLAYER']]
    except: return pd.DataFrame(columns=['PLAYER_ID', 'PLAYER'])

# 啟動同步
with st.spinner('🔄 正在學習近況趨勢與主場優勢數據...'):
    try:
        model, all_games_raw, all_player_stats, features_list = get_comprehensive_data('2025-26')
    except:
        model, all_games_raw, all_player_stats, features_list = get_comprehensive_data('2024-25')
    
    recent_dates = [datetime.now() - timedelta(days=i) for i in range(4)]
    # (省略 get_preloaded_schedules 部分，與前版相同)

# 顯示介面與對戰紀錄 (邏輯同前，但勝率計算加入 IS_HOME 參數)
# ... [省略重複的介面代碼] ...

# 關鍵勝率計算區塊優化：
# 在計算勝率時，強制賦予 Home Team 的 IS_HOME = 1，Away Team 的 IS_HOME = 0
# h_form['IS_HOME'] = 1
# a_form['IS_HOME'] = 0
# 這樣模型就會根據「這隊在主場」的歷史紀錄來加權
