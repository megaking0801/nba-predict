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

st.set_page_config(page_title="NBA 2026 智慧預測", layout="centered")
st.title("🏀 NBA 數據預測分析系統")

# 2. 初始化映射
all_teams = teams.get_teams()
team_map = {team['id']: team['abbreviation'] for team in all_teams}

# --- 核心數據與模型 (TTL = 3600) ---
@st.cache_data(ttl=3600)
def get_model_and_stats(season):
    gamefinder = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60)
    df = gamefinder.get_data_frames()
    df['GAME_DATE'] = pd.to_datetime(df['GAME_DATE'])
    df = df.sort_values(['TEAM_ID', 'GAME_DATE'])
    stats_cols = ['PTS', 'REB', 'AST', 'PLUS_MINUS']
    for col in stats_cols:
        df[f'L5_{col}'] = df.groupby('TEAM_ID')[col].transform(lambda x: x.shift(1).rolling(5).mean())
    train_df = df.dropna(subset=['L5_PTS']).copy()
    train_df['WIN'] = train_df['WL'].apply(lambda x: 1 if x == 'W' else 0)
    latest_form = df.groupby('TEAM_ABBREVIATION').tail(1)
    features = ['L5_PTS', 'L5_REB', 'L5_AST', 'L5_PLUS_MINUS']
    model = xgb.XGBClassifier(n_estimators=100, max_depth=5, eval_metric='logloss')
    model.fit(train_df[features], train_df['WIN'])
    return model, latest_form, features

# --- 預載入賽程 (TTL = 3600) ---
@st.cache_data(ttl=3600)
def get_all_schedules(date_list):
    schedules = {}
    for d_obj in date_list:
        fmt_date = d_obj.strftime('%m/%d/%Y')
        key_date = d_obj.strftime('%Y-%m-%d')
        try:
            sb = scoreboardv2.ScoreboardV2(game_date=fmt_date, timeout=20)
            df = sb.get_data_frames()
            if not df.empty:
                df['HOME_ABBR'] = df['HOME_TEAM_ID'].map(team_map)
                df['AWAY_ABBR'] = df['VISITOR_TEAM_ID'].map(team_map)
                schedules[key_date] = df[['GAME_ID', 'HOME_ABBR', 'AWAY_ABBR']].to_dict('records')
            else:
                schedules[key_date] = []
        except:
            schedules[key_date] = []
    return schedules

# 啟動同步
with st.spinner('🚀 數據同步中...'):
    try:
        model, latest_form_df, features = get_model_and_stats('2025-26')
    except:
        model, latest_form_df, features = get_model_and_stats('2024-25')
    recent_dates = [datetime.now() - timedelta(days=i) for i in range(4)]
    all_schedules = get_all_schedules(recent_dates)

# ----------------------
# 3. 標籤式 UI (取代側邊欄)
# ----------------------
st.write("### 📅 選擇比賽日期")
tab_labels = [d.strftime('%m/%d') + (" (今)" if i==0 else "") for i, d in enumerate(recent_dates)]
tabs = st.tabs(tab_labels)

for i, tab in enumerate(tabs):
    with tab:
        current_date_key = recent_dates[i].strftime('%Y-%m-%d')
        games_list = all_schedules.get(current_date_key, [])
        
        if not games_list:
            st.info(f"⚠️ 該日暫無比賽資訊")
        else:
            # 比賽選單
            game_options = [f"{TEAM_NAME_CH.get(g['AWAY_ABBR'], g['AWAY_ABBR'])} @ {TEAM_NAME_CH.get(g['HOME_ABBR'], g['HOME_ABBR'])}" for g in games_list]
            
            selected_game_idx = st.selectbox(
                "🎯 選擇一場對決", 
                range(len(game_options)), 
                format_func=lambda x: game_options[x],
                key=f"select_{current_date_key}"
            )
            
            game = games_list[selected_game_idx]
            h_abbr, a_abbr = game['HOME_ABBR'], game['AWAY_ABBR']
            
            # 獲取數據與預測
            h_stats = latest_form_df[latest_form_df['TEAM_ABBREVIATION'] == h_abbr][features]
            a_stats = latest_form_df[latest_form_df['TEAM_ABBREVIATION'] == a_abbr][features]
            
            if not h_stats.empty and not a_stats.empty:
                h_raw = model.predict_proba(h_stats)
                a_raw = model.predict_proba(a_stats)
                total = h_raw + a_raw
                h_final, a_final = (h_raw/total)*100, (a_raw/total)*100
                
                st.divider()
                col1, col2 = st.columns(2)
                with col1:
                    st.metric(f"🏠 {TEAM_NAME_CH.get(h_abbr, h_abbr)}", f"{h_final:.1f}%")
                    st.caption(f"近5均分: {h_stats['L5_PTS'].values:.1f}")
                with col2:
                    st.metric(f"✈️ {TEAM_NAME_CH.get(a_abbr, a_abbr)}", f"{a_final:.1f}%")
                    st.caption(f"近5均分: {a_stats['L5_PTS'].values:.1f}")
                
                st.divider()
                rec = h_abbr if h_final > a_final else a_abbr
                st.success(f"📌 **預測結果：{TEAM_NAME_CH.get(rec, rec)} 較具贏面**")
            else:
                st.warning("該場比賽暫無足夠數據進行預測")

# 4. 戰力排行 (放在最下方收納)
with st.expander("📊 查看全聯盟近五場戰力排行榜"):
    rank = latest_form_df.copy()
    rank['隊伍'] = rank['TEAM_ABBREVIATION'].map(TEAM_NAME_CH)
    st.dataframe(rank.sort_values('L5_PLUS_MINUS', ascending=False)[['隊伍', 'L5_PTS', 'L5_PLUS_MINUS']], hide_index=True)
