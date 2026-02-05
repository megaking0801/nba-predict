import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2
from nba_api.stats.static import teams
from nba_api.library.http import NBAStatsHTTP
import pandas as pd
import xgboost as xgb
from datetime import datetime, timedelta
import warnings

# 忽略警告
warnings.filterwarnings('ignore')

# --- 偽裝 Header 防止被 NBA 官網封鎖 ---
NBAStatsHTTP.headers = {
    'Host': 'stats.nba.com',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
    'Referer': 'https://www.nba.com',
    'Connection': 'keep-alive',
}

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

st.set_page_config(page_title="NBA 2026 智慧預測系統", layout="centered")
st.title("🏀 NBA 數據預測分析系統 (2026 版)")

# 2. 基礎資料初始化
all_teams = teams.get_teams()
team_map = {team['id']: team['abbreviation'] for team in all_teams}

@st.cache_data(ttl=3600)
def get_historical_and_train(season):
    """抓取歷史數據並計算近五場滾動平均"""
    gamefinder = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60)
    df = gamefinder.get_data_frames()[0]
    df['GAME_DATE'] = pd.to_datetime(df['GAME_DATE'])
    df = df.sort_values(['TEAM_ID', 'GAME_DATE'])
    
    # 計算近 5 場平均 (Rolling)
    stats_cols = ['PTS', 'REB', 'AST', 'PLUS_MINUS']
    for col in stats_cols:
        df[f'L5_{col}'] = df.groupby('TEAM_ID')[col].transform(lambda x: x.shift(1).rolling(5).mean())
    
    # 準備訓練集
    train_df = df.dropna(subset=['L5_PTS']).copy()
    train_df['WIN'] = train_df['WL'].apply(lambda x: 1 if x == 'W' else 0)
    
    # 取得各隊最新近況 (用於預測)
    latest_form = df.groupby('TEAM_ABBREVIATION').tail(1)
    
    return train_df, latest_form

# 初始化數據
with st.spinner('正在從 NBA 官網同步數據...'):
    try:
        df_train, latest_form_df = get_historical_and_train('2025-26')
    except:
        df_train, latest_form_df = get_historical_and_train('2024-25')

# 3. 訓練 XGBoost 模型
features = ['L5_PTS', 'L5_REB', 'L5_AST', 'L5_PLUS_MINUS']
X = df_train[features]
y = df_train['WIN']
model = xgb.XGBClassifier(n_estimators=100, max_depth=5, eval_metric='logloss')
model.fit(X, y)

# 4. 側邊欄：日期選單
st.sidebar.header("🔍 預測設定")
date_map = {}
for i in range(4):
    d = datetime.now() - timedelta(days=i)
    label = d.strftime('%Y-%m-%d') + (" (今日)" if i == 0 else "")
    date_map[label] = d

selected_label = st.sidebar.selectbox("選擇預測日期", list(date_map.keys()))
target_date_obj = date_map[selected_label]

# 5. 抓取指定日期賽程
def get_games_by_date(target_date):
    formatted_date = target_date.strftime('%m/%d/%Y')
    try:
        sb = scoreboardv2.ScoreboardV2(game_date=formatted_date)
        df = sb.get_data_frames()[0]
        if df.empty: return []
        
        df['HOME_ABBR'] = df['HOME_TEAM_ID'].map(team_map)
        df['AWAY_ABBR'] = df['VISITOR_TEAM_ID'].map(team_map)
        return df[['GAME_ID', 'HOME_ABBR', 'AWAY_ABBR']].to_dict('records')
    except:
        return []

games_list = get_games_by_date(target_date_obj)

# 6. 主要介面渲染
if not games_list:
    st.warning(f"⚠️ {selected_label} 暫無比賽資訊。")
else:
    # 建立中文對戰選單
    game_options = []
    for g in games_list:
        away_ch = TEAM_NAME_CH.get(g['AWAY_ABBR'], g['AWAY_ABBR'])
        home_ch = TEAM_NAME_CH.get(g['HOME_ABBR'], g['HOME_ABBR'])
        game_options.append(f"{away_ch} @ {home_ch}")
    
    selected_game_idx = st.selectbox(
        f"🎯 選擇比賽查看分析 ({selected_label})", 
        range(len(game_options)), 
        format_func=lambda x: game_options[x],
        key=f"select_{selected_label}" 
    )
    
    game = games_list[selected_game_idx]
    h_abbr, a_abbr = game['HOME_ABBR'], game['AWAY_ABBR']
    
    h_stats = latest_form_df[latest_form_df['TEAM_ABBREVIATION'] == h_abbr][features]
    a_stats = latest_form_df[latest_form_df['TEAM_ABBREVIATION'] == a_abbr][features]
    
    if not h_stats.empty and not a_stats.empty:
        # --- 歸一化勝率邏輯 ---
        # 取得兩隊各自贏球的原始機率 (predict_proba 會回傳 [負機率, 勝機率])
        h_raw_win_prob = model.predict_proba(h_stats)[0][1]
        a_raw_win_prob = model.predict_proba(a_stats)[0][1]
        
        # 歸一化：主隊勝率 = 主隊原始勝率 / (主隊原始勝率 + 客隊原始勝率)
        total_win_prob = h_raw_win_prob + a_raw_win_prob
        h_final_prob = h_raw_win_prob / total_win_prob
        a_final_prob = a_raw_win_prob / total_win_prob
        
        st.divider()
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown(f"### 🏠 {TEAM_NAME_CH.get(h_abbr, h_abbr)}")
            st.metric("歸一化勝率", f"{h_final_prob*100:.1f}%")
            st.write(f"🏀 近5場均分: {h_stats['L5_PTS'].values[0]:.1f}")
            st.write(f"📈 近5場正負值: {h_stats['L5_PLUS_MINUS'].values[0]:.1f}")
            
        with col2:
            st.markdown(f"### ✈️ {TEAM_NAME_CH.get(a_abbr, a_abbr)}")
            st.metric("歸一化勝率", f"{a_final_prob*100:.1f}%")
            st.write(f"🏀 近5場均分: {a_stats['L5_PTS'].values[0]:.1f}")
            st.write(f"📈 近5場正負值: {a_stats['L5_PLUS_MINUS'].values[0]:.1f}")
        
        st.divider()
        winner = h_abbr if h_final_prob > a_final_prob else a_abbr
        st.success(f"📌 **預測結果：{TEAM_NAME_CH.get(winner, winner)} 較具贏面**")
    else:
        st.info("該球隊數據分析中...")

# 7. 戰力排行
with st.expander("📊 查看聯盟目前近五場戰力排行榜"):
    rank = latest_form_df.copy()
    rank['隊伍'] = rank['TEAM_ABBREVIATION'].map(TEAM_NAME_CH)
    rank = rank.sort_values('L5_PLUS_MINUS', ascending=False)
    st.dataframe(rank[['隊伍', 'L5_PTS', 'L5_PLUS_MINUS']], hide_index=True)
