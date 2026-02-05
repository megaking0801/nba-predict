import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2, commonteamroster, leagueplayerstats
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
    'MIA': '邁阿密熱火', 'MIL': '密爾瓦基公鹿', 'MIN': '明尼蘇達灰狼',
    'NOP': '紐奧良鵜鶘', 'NYK': '紐約尼克', 'OKC': '奧克拉荷馬雷霆',
    'ORL': '奧蘭多魔術', 'PHI': '費城 76 人', 'PHX': '鳳凰城太陽',
    'POR': '波特蘭開拓者', 'SAC': '沙加緬度國王', 'SAS': '聖安東尼奧馬刺',
    'TOR': '多倫多暴龍', 'UTA': '猶他爵士', 'WAS': '華盛頓巫師'
}

st.set_page_config(page_title="NBA 2026 終極預測系統", layout="centered")
st.title("🏀 NBA 數據預測 (B2B 強化版)")

# 2. 基礎資料初始化
all_teams = teams.get_teams()
team_map = {team['id']: team['abbreviation'] for team in all_teams}

@st.cache_data(ttl=3600)
def get_comprehensive_data(season):
    # 獲取所有歷史比賽
    gamefinder = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60)
    all_games = gamefinder.get_data_frames()[0]
    all_games['GAME_DATE'] = pd.to_datetime(all_games['GAME_DATE'])
    all_games = all_games.sort_values(['TEAM_ID', 'GAME_DATE'])

    # --- 重要：計算歷史比賽中的 B2B ---
    # 計算每場比賽與上一場的間隔天數
    all_games['DAYS_REST'] = all_games.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days
    all_games['B2B'] = (all_games['DAYS_REST'] == 1).astype(int)

    # 定義戰力特徵
    stats_cols = ['PTS', 'PLUS_MINUS', 'FG_PCT', 'FG3_PCT', 'OREB', 'TOV']
    for col in stats_cols:
        all_games[f'L5_{col}'] = all_games.groupby('TEAM_ID')[col].transform(lambda x: x.shift(1).rolling(5).mean())
    
    # 訓練模型 (包含 B2B 特徵)
    train_df = all_games.dropna(subset=['L5_PTS']).copy()
    train_df['WIN'] = train_df['WL'].apply(lambda x: 1 if x == 'W' else 0)
    
    features = [f'L5_{c}' for c in stats_cols] + ['B2B']
    model = xgb.XGBClassifier(n_estimators=150, max_depth=5, eval_metric='logloss')
    model.fit(train_df[features], train_df['WIN'])

    # 獲取全聯盟球員統計
    player_stats = leagueplayerstats.LeaguePlayerStats(season=season).get_data_frames()[0]
    player_stats = player_stats[['PLAYER_ID', 'PLAYER_NAME', 'PTS']]

    return model, all_games, player_stats, features

@st.cache_data(ttl=3600)
def get_team_roster_names(team_id):
    roster = commonteamroster.CommonTeamRoster(team_id=team_id).get_data_frames()[0]
    return roster[['PLAYER_ID', 'PLAYER', 'POSITION']]

@st.cache_data(ttl=3600)
def get_all_schedules(date_list):
    schedules = {}
    for d_obj in date_list:
        fmt_date = d_obj.strftime('%m/%d/%Y')
        key_date = d_obj.strftime('%Y-%m-%d')
        try:
            sb = scoreboardv2.ScoreboardV2(game_date=fmt_date, timeout=20)
            df = sb.get_data_frames()[0]
            if not df.empty:
                df['HOME_ABBR'] = df['HOME_TEAM_ID'].map(team_map)
                df['AWAY_ABBR'] = df['VISITOR_TEAM_ID'].map(team_map)
                schedules[key_date] = df[['GAME_ID', 'HOME_TEAM_ID', 'VISITOR_TEAM_ID', 'HOME_ABBR', 'AWAY_ABBR']].to_dict('records')
            else: schedules[key_date] = []
        except: schedules[key_date] = []
    return schedules

# 啟動初始化
with st.spinner('🚀 2026 數據分析中...'):
    try:
        model, all_games_raw, all_player_stats, features_list = get_comprehensive_data('2025-26')
    except:
        model, all_games_raw, all_player_stats, features_list = get_comprehensive_data('2024-25')
    
    recent_dates = [datetime.now() - timedelta(days=i) for i in range(4)]
    all_schedules = get_all_schedules(recent_dates)

# ----------------------
# 3. 標籤式 UI
# ----------------------
st.write("### 📅 比賽日期 (2026)")
tab_labels = [d.strftime('%m/%d') + (" (今)" if i==0 else "") for i, d in enumerate(recent_dates)]
tabs = st.tabs(tab_labels)

for i, tab in enumerate(tabs):
    with tab:
        curr_date_dt = recent_dates[i]
        date_key = curr_date_dt.strftime('%Y-%m-%d')
        games = all_schedules.get(date_key, [])
        
        if not games:
            st.info("⚠️ 暫無賽程")
        else:
            options = [f"{TEAM_NAME_CH.get(g['AWAY_ABBR'], g['AWAY_ABBR'])} @ {TEAM_NAME_CH.get(g['HOME_ABBR'], g['HOME_ABBR'])}" for g in games]
            sel_idx = st.selectbox("🎯 選擇對決", range(len(options)), format_func=lambda x: options[x], key=f"tab_{date_key}")
            
            sel_game = games[sel_idx]
            h_id, a_id = sel_game['HOME_TEAM_ID'], sel_game['VISITOR_TEAM_ID']
            h_abbr, a_abbr = sel_game['HOME_ABBR'], sel_game['AWAY_ABBR']
            
            # --- 數據準備 ---
            # 取得該隊最後一場比賽數據作為基礎
            h_form = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == h_abbr].tail(1)[features_list].copy()
            a_form = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == a_abbr].tail(1)[features_list].copy()
            
            # --- 重要：即時判定今日是否為 B2B ---
            h_last_date = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == h_abbr]['GAME_DATE'].max()
            a_last_date = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == a_abbr]['GAME_DATE'].max()
            
            # 如果「所選日期」與「該隊最後一場比賽」只差 1 天，標記為 B2B
            h_is_b2b = 1 if (curr_date_dt.date() - h_last_date.date()).days == 1 else 0
            a_is_b2b = 1 if (curr_date_dt.date() - a_last_date.date()).days == 1 else 0
            
            h_form['B2B'] = h_is_b2b
            a_form['B2B'] = a_is_b2b

            # 對戰紀錄 H2H
            h2h_games = all_games_raw[(all_games_raw['TEAM_ABBREVIATION'] == h_abbr) & (all_games_raw['MATCHUP'].str.contains(a_abbr))]
            h_wins = len(h2h_games[h2h_games['WL'] == 'W'])
            a_wins = len(h2h_games[h2h_games['WL'] == 'L'])

            # 執行預測
            h_p = float(model.predict_proba(h_form)[:, 1]) + (h_wins - a_wins) * 0.02
            a_p = float(model.predict_proba(a_form)[:, 1]) + (a_wins - h_wins) * 0.02
            h_final, a_final = (h_p/(h_p+a_p))*100, (a_p/(h_p+a_p))*100

            # UI 顯示
            st.divider()
            st.info(f"⚔️ 本季對戰：{h_abbr} {h_wins}勝 - {a_wins}勝 {a_abbr}")
            col1, col2 = st.columns(2)
            with col1:
                st.metric(TEAM_NAME_CH.get(h_abbr, h_abbr), f"{h_final:.1f}%")
                if h_is_b2b: st.warning("⚠️ 背靠背作戰 (B2B)")
            with col2:
                st.metric(TEAM_NAME_CH.get(a_abbr, a_abbr), f"{a_final:.1f}%")
                if a_is_b2b: st.warning("⚠️ 背靠背作戰 (B2B)")
            
            st.write("#### 👤 核心名單 (2026)")
            h_list = get_team_roster_names(h_id).head(5).merge(all_player_stats, left_on='PLAYER', right_on='PLAYER_NAME', how='left')
            a_list = get_team_roster_names(a_id).head(5).merge(all_player_stats, left_on='PLAYER', right_on='PLAYER_NAME', how='left')
            c_h, c_a = st.columns(2)
            with c_h: st.dataframe(h_list[['PLAYER', 'PTS']].rename(columns={'PLAYER':'姓名','PTS':'均分'}), hide_index=True)
            with c_a: st.dataframe(a_list[['PLAYER', 'PTS']].rename(columns={'PLAYER':'姓名','PTS':'均分'}), hide_index=True)

            st.divider()
            st.success(f"📌 推薦獲勝：{TEAM_NAME_CH.get(h_abbr if h_final > a_final else a_abbr)}")
