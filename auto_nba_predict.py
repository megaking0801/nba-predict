import streamlit as st
# 修正後的端點名稱：leaguedashplayerstats
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2, commonteamroster, leaguedashplayerstats
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
from datetime import datetime, timedelta
import warnings

# 忽略警告
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
st.title("🏀 NBA 數據預測 (終極完全體)")

# 2. 基礎資料初始化
all_teams = teams.get_teams()
team_map = {team['id']: team['abbreviation'] for team in all_teams}

@st.cache_data(ttl=3600)
def get_comprehensive_data(season):
    # 獲取球隊歷史數據
    gamefinder = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60)
    all_games = gamefinder.get_data_frames()[0]
    all_games['GAME_DATE'] = pd.to_datetime(all_games['GAME_DATE'])
    all_games = all_games.sort_values(['TEAM_ID', 'GAME_DATE'])

    # 計算歷史 B2B 與 命中率數據
    all_games['DAYS_REST'] = all_games.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days
    all_games['B2B'] = (all_games['DAYS_REST'] == 1).astype(int)
    
    stats_cols = ['PTS', 'PLUS_MINUS', 'FG_PCT', 'FG3_PCT', 'OREB', 'TOV']
    for col in stats_cols:
        all_games[f'L5_{col}'] = all_games.groupby('TEAM_ID')[col].transform(lambda x: x.shift(1).rolling(5).mean())
    
    # 訓練模型
    train_df = all_games.dropna(subset=['L5_PTS']).copy()
    train_df['WIN'] = train_df['WL'].apply(lambda x: 1 if x == 'W' else 0)
    features = [f'L5_{c}' for c in stats_cols] + ['B2B']
    
    model = xgb.XGBClassifier(n_estimators=100, max_depth=5, eval_metric='logloss')
    model.fit(train_df[features], train_df['WIN'])

    # 獲取全聯盟球員統計
    player_stats = leaguedashplayerstats.LeagueDashPlayerStats(season=season).get_data_frames()[0]
    player_stats = player_stats[['PLAYER_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST']]

    return model, all_games, player_stats, features

@st.cache_data(ttl=3600)
def get_team_roster_names(team_id):
    roster = commonteamroster.CommonTeamRoster(team_id=team_id).get_data_frames()[0]
    return roster[['PLAYER_ID', 'PLAYER', 'POSITION']]

@st.cache_data(ttl=3600)
def get_preloaded_schedules(date_list):
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

# 啟動同步
with st.spinner('🚀 正在同步 2026 NBA 大數據 (H2H, B2B, 名單)...'):
    try:
        # 嘗試獲取 2025-26 賽季數據
        model, all_games_raw, all_player_stats, features_list = get_comprehensive_data('2025-26')
    except:
        model, all_games_raw, all_player_stats, features_list = get_comprehensive_data('2024-25')
    
    recent_dates = [datetime.now() - timedelta(days=i) for i in range(4)]
    all_schedules = get_preloaded_schedules(recent_dates)

# 3. 標籤式 UI (手機優化)
st.write("### 📅 選擇比賽日期")
tab_labels = [d.strftime('%m/%d') + (" (今)" if i==0 else "") for i, d in enumerate(recent_dates)]
tabs = st.tabs(tab_labels)

for i, tab in enumerate(tabs):
    with tab:
        curr_date_dt = recent_dates[i]
        date_key = curr_date_dt.strftime('%Y-%m-%d')
        games = all_schedules.get(date_key, [])
        
        if not games:
            st.info("⚠️ 該日暫無賽程資訊")
        else:
            options = [f"{TEAM_NAME_CH.get(g['AWAY_ABBR'], g['AWAY_ABBR'])} @ {TEAM_NAME_CH.get(g['HOME_ABBR'], g['HOME_ABBR'])}" for g in games]
            sel_idx = st.selectbox("🎯 選擇一場對決分析", range(len(options)), format_func=lambda x: options[x], key=f"tab_{date_key}")
            
            sel_game = games[sel_idx]
            h_id, a_id = sel_game['HOME_TEAM_ID'], sel_game['VISITOR_TEAM_ID']
            h_abbr, a_abbr = sel_game['HOME_ABBR'], sel_game['AWAY_ABBR']
            
            # --- 數據準備與 B2B 動態判定 ---
            h_form = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == h_abbr].tail(1)[features_list].copy()
            a_form = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == a_abbr].tail(1)[features_list].copy()
            
            h_last = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == h_abbr]['GAME_DATE'].max()
            a_last = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == a_abbr]['GAME_DATE'].max()
            h_is_b2b = 1 if (curr_date_dt.date() - h_last.date()).days == 1 else 0
            a_is_b2b = 1 if (curr_date_dt.date() - a_last.date()).days == 1 else 0
            h_form['B2B'] = h_is_b2b
            a_form['B2B'] = a_is_b2b

            # 對戰紀錄 H2H
            h2h_df = all_games_raw[(all_games_raw['TEAM_ABBREVIATION'] == h_abbr) & (all_games_raw['MATCHUP'].str.contains(a_abbr))]
            h_wins = len(h2h_df[h2h_df['WL'] == 'W'])
            a_wins = len(h2h_df[h2h_df['WL'] == 'L'])

            # 預測
            h_p = float(model.predict_proba(h_form)[:, 1]) + (h_wins - a_wins) * 0.03
            a_p = float(model.predict_proba(a_form)[:, 1]) + (a_wins - h_wins) * 0.03
            total = h_p + a_p
            h_f, a_f = (h_p/total)*100, (a_p/total)*100

            # UI 顯示
            st.divider()
            st.info(f"⚔️ 賽季對戰：{h_abbr} {h_wins}勝 - {a_wins}勝 {a_abbr}")
            c1, c2 = st.columns(2)
            with c1:
                st.metric(TEAM_NAME_CH.get(h_abbr, h_abbr), f"{h_f:.1f}%")
                if h_is_b2b: st.warning("⚠️ 背靠背 (B2B)")
            with c2:
                st.metric(TEAM_NAME_CH.get(a_abbr, a_abbr), f"{a_f:.1f}%")
                if a_is_b2b: st.warning("⚠️ 背靠背 (B2B)")
            
            # --- 重點修改：最新球員名單 (按場均得分排序) ---
            st.write("#### 👤 核心球員名單 (依場均得分排序)")
            try:
                # 獲取雙方名單
                h_roster = get_team_roster_names(h_id)
                a_roster = get_team_roster_names(a_id)

                # 合併得分數據並排序
                h_list = h_roster.merge(all_player_stats, left_on='PLAYER', right_on='PLAYER_NAME', how='left')
                h_list = h_list.sort_values(by='PTS', ascending=False).head(5)

                a_list = a_roster.merge(all_player_stats, left_on='PLAYER', right_on='PLAYER_NAME', how='left')
                a_list = a_list.sort_values(by='PTS', ascending=False).head(5)

                ch, ca = st.columns(2)
                with ch: 
                    st.dataframe(h_list[['PLAYER', 'PTS']].rename(columns={'PLAYER':'姓名','PTS':'均分'}), hide_index=True, use_container_width=True)
                with ca: 
                    st.dataframe(a_list[['PLAYER', 'PTS']].rename(columns={'PLAYER':'姓名','PTS':'均分'}), hide_index=True, use_container_width=True)
            except Exception as e:
                st.caption(f"球員名單同步中或發生錯誤... {e}")

            st.divider()
            st.success(f"📌 系統推薦：{TEAM_NAME_CH.get(h_abbr if h_f > a_f else a_abbr)}")
