import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2, commonteamroster, leaguedashplayerstats
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import plotly.express as px
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

st.set_page_config(page_title="NBA 2026 終極交易預測系統", layout="wide")
st.title("🏀 NBA 數據預測 (含手感趨勢分析)")

# 2. 基礎資料初始化
all_teams = teams.get_teams()
team_map = {team['id']: team['abbreviation'] for team in all_teams}

@st.cache_data(ttl=600)
def get_comprehensive_data(season):
    # 球隊歷史數據
    gamefinder = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60)
    all_games = gamefinder.get_data_frames()[0]
    all_games['GAME_DATE'] = pd.to_datetime(all_games['GAME_DATE'])
    all_games = all_games.sort_values(['TEAM_ID', 'GAME_DATE'])

    # 特徵工程
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

    # 全賽季場均統計 (含 TEAM_ID 驗證)
    player_stats = leaguedashplayerstats.LeagueDashPlayerStats(season=season, per_mode_detailed='PerGame').get_data_frames()[0]
    player_stats = player_stats[['PLAYER_ID', 'PLAYER_NAME', 'TEAM_ID', 'PTS', 'REB', 'AST', 'STL']]

    return model, all_games, player_stats, features

@st.cache_data(ttl=600)
def get_team_roster(team_id):
    try:
        roster = commonteamroster.CommonTeamRoster(team_id=team_id).get_data_frames()[0]
        return roster[['PLAYER_ID', 'PLAYER']]
    except:
        return pd.DataFrame(columns=['PLAYER_ID', 'PLAYER'])

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
with st.spinner('🚀 正在載入最新名單與手感數據...'):
    try:
        model, all_games_raw, all_player_stats, features_list = get_comprehensive_data('2025-26')
    except:
        model, all_games_raw, all_player_stats, features_list = get_comprehensive_data('2024-25')
    
    recent_dates = [datetime.now() - timedelta(days=i) for i in range(4)]
    all_schedules = get_preloaded_schedules(recent_dates)

# 3. UI 邏輯
st.write("### 📅 比賽日選擇")
tabs = st.tabs([d.strftime('%m/%d') for d in recent_dates])

for i, tab in enumerate(tabs):
    with tab:
        curr_date_dt = recent_dates[i]
        date_key = curr_date_dt.strftime('%Y-%m-%d')
        games = all_schedules.get(date_key, [])
        
        if not games:
            st.info("⚠️ 該日無賽程")
        else:
            options = [f"{TEAM_NAME_CH.get(g['AWAY_ABBR'], g['AWAY_ABBR'])} @ {TEAM_NAME_CH.get(g['HOME_ABBR'], g['HOME_ABBR'])}" for g in games]
            sel_idx = st.selectbox("🎯 選擇場次", range(len(options)), format_func=lambda x: options[x], key=f"tab_{date_key}")
            
            sel_game = games[sel_idx]
            h_id, a_id = sel_game['HOME_TEAM_ID'], sel_game['VISITOR_TEAM_ID']
            h_abbr, a_abbr = sel_game['HOME_ABBR'], sel_game['AWAY_ABBR']
            
            # 模型勝率預測
            h_form = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == h_abbr].tail(1)[features_list].copy()
            a_form = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == a_abbr].tail(1)[features_list].copy()
            h_p = float(model.predict_proba(h_form)[:, 1])
            a_p = float(model.predict_proba(a_form)[:, 1])
            h_f, a_f = (h_p/(h_p+a_p))*100, (a_p/(h_p+a_p))*100

            st.divider()
            c1, c2 = st.columns(2)
            c1.metric(f"🏠 {TEAM_NAME_CH.get(h_abbr, h_abbr)} 勝率", f"{h_f:.1f}%")
            c2.metric(f"✈️ {TEAM_NAME_CH.get(a_abbr, a_abbr)} 勝率", f"{a_f:.1f}%")
            
            # --- 核心球員名單與防交易過期邏輯 ---
            def get_final_roster(t_id):
                roster = get_team_roster(t_id)
                merged = roster.merge(all_player_stats, on='PLAYER_ID', how='left')
                # 核心驗證：如果統計數據的隊伍ID不符且已有打過球(非0)，則視為離隊
                cleaned = merged[~((merged['TEAM_ID'] != t_id) & (merged['TEAM_ID'] != 0) & (merged['TEAM_ID'].notnull()))]
                return cleaned.sort_values(by='PTS', ascending=False).head(5)

            h_list = get_final_roster(h_id)
            a_list = get_final_roster(a_id)

            st.write("#### 👤 核心球員數據 (跨隊場均統計)")
            display_cols = {'PLAYER': '姓名', 'PTS': '得分', 'REB': '籃板', 'AST': '助攻', 'STL': '抄截'}
            ch, ca = st.columns(2)
            with ch:
                st.dataframe(h_list[list(display_cols.keys())].rename(columns=display_cols), hide_index=True, use_container_width=True)
            with ca:
                st.dataframe(a_list[list(display_cols.keys())].rename(columns=display_cols), hide_index=True, use_container_width=True)

            # --- 加分項：手感趨勢分析圖 ---
            st.write("#### 🔥 兩隊得分王手感熱度 (本季平均 vs. 近期表現)")
            try:
                # 抓取兩隊第一名得分王數據
                star_players = pd.concat([h_list.head(1), a_list.head(1)])
                star_players['隊伍'] = [TEAM_NAME_CH.get(h_abbr), TEAM_NAME_CH.get(a_abbr)]
                
                fig = px.bar(star_players, x='PLAYER', y='PTS', color='隊伍', 
                             title="兩隊得分核心戰力比拼",
                             labels={'PTS':'場均得分', 'PLAYER':'球員名稱'},
                             text_auto='.1f', barmode='group')
                st.plotly_chart(fig, use_container_width=True)
            except:
                st.caption("暫無手感趨勢圖表數據")

            st.success(f"📌 系統推薦預測結果：{TEAM_NAME_CH.get(h_abbr if h_f > a_f else a_abbr)}")
