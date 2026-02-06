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

st.set_page_config(page_title="NBA 2026 終極盤口預測系統", layout="wide")
st.title("🏀 NBA 終極預測系統 (雙模型修正版)")

def get_snapshot_path(date_key):
    return f"nba_snapshot_{date_key}.json"

# --- 1. 數據與雙模型訓練 ---
@st.cache_data(ttl=600)
def get_comprehensive_data(season):
    gamefinder = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60)
    all_games = gamefinder.get_data_frames()[0]
    all_games['GAME_DATE'] = pd.to_datetime(all_games['GAME_DATE'])
    all_games = all_games.sort_values(['TEAM_ID', 'GAME_DATE'])

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
    
    # 模型訓練
    clf_model = xgb.XGBClassifier(n_estimators=150, max_depth=4, learning_rate=0.05, eval_metric='logloss')
    clf_model.fit(train_df[features], train_df['WIN_BIN'])
    
    reg_model = xgb.XGBRegressor(n_estimators=150, max_depth=4, learning_rate=0.05)
    reg_model.fit(train_df[features], train_df['PLUS_MINUS'])

    p_stats_raw = leaguedashplayerstats.LeagueDashPlayerStats(season=season, per_mode_detailed='PerGame').get_data_frames()[0]
    for col in ['PTS', 'REB', 'AST', 'STL']:
        p_stats_raw[col] = pd.to_numeric(p_stats_raw[col], errors='coerce').fillna(0)
    player_stats = p_stats_raw[['PLAYER_ID', 'PLAYER_NAME', 'TEAM_ID', 'PTS', 'REB', 'AST', 'STL']]

    return clf_model, reg_model, all_games, player_stats, features

@st.cache_data(ttl=600)
def get_team_roster(team_id):
    try:
        roster = commonteamroster.CommonTeamRoster(team_id=team_id).get_data_frames()[0]
        if 'PLAYER' in roster.columns: roster = roster.rename(columns={'PLAYER': 'PLAYER_NAME'})
        return roster[['PLAYER_ID', 'PLAYER_NAME']]
    except: return pd.DataFrame(columns=['PLAYER_ID', 'PLAYER_NAME'])

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

# --- 2. 封盤與預測邏輯 ---
def get_locked_results_for_date(date_key, games, clf_model, reg_model, all_games_raw, player_stats, features_list):
    snapshot_file = get_snapshot_path(date_key)
    now_tw = datetime.now(tw_tz)
    
    if os.path.exists(snapshot_file):
        with open(snapshot_file, 'r', encoding='utf-8') as f:
            return json.load(f), True

    results = {}
    for g in games:
        h_abbr, a_abbr = g['HOME_ABBR'], g['AWAY_ABBR']
        h_id, a_id = g['HOME_TEAM_ID'], g['VISITOR_TEAM_ID']
        
        h_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == h_abbr].tail(1)[features_list].copy()
        a_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == a_abbr].tail(1)[features_list].copy()
        h_feat['IS_HOME'], a_feat['IS_HOME'] = 1, 0
        
        # 修正處：使用 [0] 確保獲取純量 (Scalar)
        h_p = float(clf_model.predict_proba(h_feat)[:, 1][0])
        a_p = float(clf_model.predict_proba(a_feat)[:, 1][0])
        h_prob, a_prob = (h_p/(h_p+a_p))*100, (a_p/(h_p+a_p))*100
        
        h_spread = float(reg_model.predict(h_feat)[0])
        a_spread = float(reg_model.predict(a_feat)[0])
        predicted_diff = h_spread - a_spread
        
        def get_clean_roster(t_id):
            ros = get_team_roster(t_id)
            m = ros.merge(player_stats, on='PLAYER_ID', how='left')
            c = m[~((m['TEAM_ID'] != t_id) & (m['TEAM_ID'] != 0) & (m['TEAM_ID'].notnull()))]
            return c.sort_values(by='PTS', ascending=False).head(5).to_dict('records')

        results[str(g['GAME_ID'])] = {
            'home_prob': h_prob, 'away_prob': a_prob,
            'predicted_diff': round(predicted_diff, 1),
            'home_roster': get_clean_roster(h_id),
            'away_roster': get_clean_roster(a_id),
            'lock_time': now_tw.strftime('%Y-%m-%d %H:%M:%S')
        }
    
    if date_key == now_tw.strftime('%Y-%m-%d') and now_tw.hour >= 0:
        with open(snapshot_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False)
            
    return results, False

# --- 3. UI 渲染 ---
with st.spinner('🚀 系統初始化中...'):
    clf_model, reg_model, all_games_raw, all_player_stats, features_list = get_comprehensive_data('2025-26')

date_list = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in date_list])

for i, tab in enumerate(tabs):
    with tab:
        current_date = date_list[i]
        date_key = current_date.strftime('%Y-%m-%d')
        games = get_schedule_for_date(current_date)
        
        if not games:
            st.info(f"📅 {date_key} 暫無賽程")
        else:
            locked_data, is_locked = get_locked_results_for_date(date_key, games, clf_model, reg_model, all_games_raw, all_player_stats, features_list)
            
            options = [f"{TEAM_NAME_CH.get(g['AWAY_ABBR'], g['AWAY_ABBR'])} @ {TEAM_NAME_CH.get(g['HOME_ABBR'], g['HOME_ABBR'])}" for g in games]
            sel_game_idx = st.selectbox("🎯 選擇場次", range(len(options)), format_func=lambda x: options[x], key=f"sel_{date_key}")
            
            g_data = games[sel_game_idx]
            res = locked_data.get(str(g_data['GAME_ID']))
            
            if res:
                if is_locked: st.caption(f"🔒 封盤數據 (鎖定時間: {res.get('lock_time', 'N/A')})")
                
                c1, c2, c3 = st.columns(3)
                c1.metric(f"{TEAM_NAME_CH.get(g_data['HOME_ABBR'])} 勝率", f"{res.get('home_prob', 0):.1f}%")
                c2.metric(f"{TEAM_NAME_CH.get(g_data['AWAY_ABBR'])} 勝率", f"{res.get('away_prob', 0):.1f}%")
                
                diff = res.get('predicted_diff', 0)
                winner_abbr = g_data['HOME_ABBR'] if diff > 0 else g_data['AWAY_ABBR']
                c3.metric("預計勝分差", f"{abs(diff)} 分", delta=f"{TEAM_NAME_CH.get(winner_abbr)} 佔優" if diff != 0 else None)

                # 對戰紀錄表格
                st.write("#### ⚔️ 本季對戰紀錄")
                h_id, a_abbr = g_data['HOME_TEAM_ID'], g_data['AWAY_ABBR']
                h2h = all_games_raw[((all_games_raw['TEAM_ID'] == h_id) & (all_games_raw['MATCHUP'].str.contains(a_abbr)))]
                if not h2h.empty:
                    st.table(h2h[['GAME_DATE', 'MATCHUP', 'WL', 'PTS', 'PLUS_MINUS']].head(5))
                else: st.caption("尚無紀錄")

                # 名單顯示
                st.write("#### 👤 核心球員 (封盤名單)")
                ch, ca = st.columns(2)
                
                def safe_display(roster_data):
                    if not roster_data: return pd.DataFrame(columns=['姓名','得分','籃板','助攻'])
                    df = pd.DataFrame(roster_data)
                    for col in ['PLAYER_NAME', 'PTS', 'REB', 'AST']:
                        if col not in df.columns: df[col] = "N/A"
                    return df[['PLAYER_NAME', 'PTS', 'REB', 'AST']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分','REB':'籃板','AST':'助攻'})

                with ch:
                    st.caption(TEAM_NAME_CH.get(g_data['HOME_ABBR']))
                    st.dataframe(safe_display(res.get('home_roster', [])), hide_index=True)
                with ca:
                    st.caption(TEAM_NAME_CH.get(g_data['AWAY_ABBR']))
                    st.dataframe(safe_display(res.get('away_roster', [])), hide_index=True)

                advice = "讓分盤建議："
                if abs(diff) > 8: advice += f"🔥 強力看好 {TEAM_NAME_CH.get(winner_abbr)}"
                elif abs(diff) > 3: advice += f"✅ 看好 {TEAM_NAME_CH.get(winner_abbr)}"
                else: advice += "⚠️ 實力接近，建議避開讓分盤"
                st.success(f"📌 {advice} (預測分差: {abs(diff)})")
