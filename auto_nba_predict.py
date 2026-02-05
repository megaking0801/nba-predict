import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2
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
st.title("🏀 NBA 數據預測 (含對戰紀錄與命中率)")

# 2. 基礎資料初始化
all_teams = teams.get_teams()
team_map = {team['id']: team['abbreviation'] for team in all_teams}

@st.cache_data(ttl=3600)
def get_advanced_data(season):
    # 獲取所有比賽
    gamefinder = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60)
    all_games = gamefinder.get_data_frames()[0]
    all_games['GAME_DATE'] = pd.to_datetime(all_games['GAME_DATE'])
    all_games = all_games.sort_values(['TEAM_ID', 'GAME_DATE'])

    # 定義特徵：得分、正負值、投籃%、三分%、罰球%、進攻籃板、失誤
    stats_cols = ['PTS', 'PLUS_MINUS', 'FG_PCT', 'FG3_PCT', 'FT_PCT', 'OREB', 'TOV']
    
    # 計算近 5 場滾動平均
    for col in stats_cols:
        all_games[f'L5_{col}'] = all_games.groupby('TEAM_ID')[col].transform(lambda x: x.shift(1).rolling(5).mean())
    
    # 背靠背 (B2B) 判定
    all_games['DAYS_REST'] = all_games.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days
    all_games['B2B'] = all_games['DAYS_REST'].apply(lambda x: 1 if x == 1 else 0)

    # 訓練模型
    train_df = all_games.dropna(subset=['L5_PTS']).copy()
    train_df['WIN'] = train_df['WL'].apply(lambda x: 1 if x == 'W' else 0)
    
    features = [f'L5_{c}' for c in stats_cols] + ['B2B']
    model = xgb.XGBClassifier(n_estimators=150, max_depth=5, eval_metric='logloss')
    model.fit(train_df[features], train_df['WIN'])

    return model, all_games, features

# 執行初始化
with st.spinner('🚀 正在深度分析賽程與對戰歷史...'):
    model, all_games_raw, features_list = get_advanced_data('2025-26')
    recent_dates = [datetime.now() - timedelta(days=i) for i in range(4)]

# ----------------------
# 3. 核心邏輯：計算對戰紀錄 (H2H)
# ----------------------
def get_h2h_analysis(team_a, team_b):
    """計算本賽季 A 隊對戰 B 隊的紀錄"""
    # 在 MATCHUP 欄位尋找包含對手縮寫的比賽
    h2h_games = all_games_raw[
        (all_games_raw['TEAM_ABBREVIATION'] == team_a) & 
        (all_games_raw['MATCHUP'].str.contains(team_b))
    ]
    wins = len(h2h_games[h2h_games['WL'] == 'W'])
    losses = len(h2h_games[h2h_games['WL'] == 'L'])
    return wins, losses

# ----------------------
# 4. UI 渲染 (Tabs)
# ----------------------
tab_labels = [d.strftime('%m/%d') + (" (今)" if i==0 else "") for i, d in enumerate(recent_dates)]
tabs = st.tabs(tab_labels)

for i, tab in enumerate(tabs):
    with tab:
        curr_date = recent_dates[i]
        fmt_date = curr_date.strftime('%m/%d/%Y')
        
        try:
            sb = scoreboardv2.ScoreboardV2(game_date=fmt_date, timeout=20)
            df = sb.get_data_frames()[0]
            if df.empty:
                st.info("⚠️ 該日暫無比賽資訊")
                continue
            
            df['HOME_ABBR'] = df['HOME_TEAM_ID'].map(team_map)
            df['AWAY_ABBR'] = df['VISITOR_TEAM_ID'].map(team_map)
            games = df.to_dict('records')
            
            options = [f"{TEAM_NAME_CH.get(g['AWAY_ABBR'], g['AWAY_ABBR'])} @ {TEAM_NAME_CH.get(g['HOME_ABBR'], g['HOME_ABBR'])}" for g in games]
            idx = st.selectbox("🎯 選擇對決", range(len(options)), format_func=lambda x: options[x], key=f"tab_{i}")
            
            sel_game = games[idx]
            h_abbr, a_abbr = sel_game['HOME_ABBR'], sel_game['AWAY_ABBR']
            
            # --- 數據準備 ---
            h_form = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == h_abbr].tail(1)[features_list].copy()
            a_form = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == a_abbr].tail(1)[features_list].copy()
            
            # 更新 B2B 狀態 (今日是否為 B2B)
            h_last_date = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == h_abbr]['GAME_DATE'].max()
            a_last_date = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == a_abbr]['GAME_DATE'].max()
            h_form['B2B'] = 1 if (curr_date - h_last_date).days == 1 else 0
            a_form['B2B'] = 1 if (curr_date - a_last_date).days == 1 else 0
            
            # 對戰紀錄
            h_wins, h_losses = get_h2h_analysis(h_abbr, a_abbr)
            
            # --- 勝率預測 ---
            h_p = float(model.predict_proba(h_form)[:, 1])
            a_p = float(model.predict_proba(a_form)[:, 1])
            
            # H2H 微調：每多贏一場對戰，勝率權重增加 3%
            h_p += (h_wins - h_losses) * 0.03
            
            h_final = (h_p / (h_p + a_p)) * 100
            a_final = (a_p / (h_p + a_p)) * 100

            # --- UI 顯示 ---
            st.divider()
            # 顯示 H2H 區塊
            st.info(f"⚔️ **本賽季對戰紀錄：{TEAM_NAME_CH[h_abbr]} {h_wins}勝 - {h_losses}勝 {TEAM_NAME_CH[a_abbr]}**")
            
            c1, c2 = st.columns(2)
            with c1:
                st.metric(TEAM_NAME_CH[h_abbr], f"{h_final:.1f}%")
                st.caption(f"🎯 命中率: {h_form['L5_FG_PCT'].values[0]*100:.1f}%")
                if h_form['B2B'].values[0]: st.warning("⚠️ 背靠背 (B2B)")
            with c2:
                st.metric(TEAM_NAME_CH[a_abbr], f"{a_final:.1f}%")
                st.caption(f"🎯 命中率: {a_form['L5_FG_PCT'].values[0]*100:.1f}%")
                if a_form['B2B'].values[0]: st.warning("⚠️ 背靠背 (B2B)")
            
            st.divider()
            # 數據對照表
            compare_df = pd.DataFrame({
                '指標 (近5場)': ['場均得分', '場均正負', '三分命中率', '場均失誤'],
                TEAM_NAME_CH[h_abbr]: [f"{h_form['L5_PTS'].values[0]:.1f}", f"{h_form['L5_PLUS_MINUS'].values[0]:+.1f}", f"{h_form['L5_FG3_PCT'].values[0]*100:.1f}%", f"{h_form['L5_TOV'].values[0]:.1f}"],
                TEAM_NAME_CH[a_abbr]: [f"{a_form['L5_PTS'].values[0]:.1f}", f"{a_form['L5_PLUS_MINUS'].values[0]:+.1f}", f"{a_form['L5_FG3_PCT'].values[0]*100:.1f}%", f"{a_form['L5_TOV'].values[0]:.1f}"]
            })
            st.table(compare_df)
            
            winner = h_abbr if h_final > a_final else a_abbr
            st.success(f"📌 **系統推薦：{TEAM_NAME_CH[winner]}**")

        except Exception as e:
            st.write("正在獲取最新對戰數據...")
