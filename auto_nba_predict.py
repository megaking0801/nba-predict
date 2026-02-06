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

st.set_page_config(page_title="NBA 2026 深度分析系統 v3", layout="wide")
st.title("🏀 NBA 終極預測系統 (邏輯統一修正版)")

def get_snapshot_path(date_key):
    return f"nba_snapshot_{date_key}.json"

# --- 1. 數據與雙模型訓練 ---
@st.cache_data(ttl=600)
def get_comprehensive_data(season):
    for _ in range(3):
        try:
            gamefinder = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60)
            all_games = gamefinder.get_data_frames()[0]
            break
        except: time.sleep(2)
    
    all_games['GAME_DATE'] = pd.to_datetime(all_games['GAME_DATE'])
    all_games = all_games.sort_values(['TEAM_ID', 'GAME_DATE'])

    all_games['IS_HOME'] = all_games['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    all_games['WIN_BIN'] = all_games['WL'].apply(lambda x: 1 if x == 'W' else 0)
    all_games['L10_WIN_RATE'] = all_games.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10).mean())
    all_games['OPP_PTS'] = all_games['PTS'] - all_games['PLUS_MINUS']
    all_games['SCORE_DISPLAY'] = all_games.apply(lambda r: f"{int(r['PTS'])} - {int(r['OPP_PTS'])}", axis=1)

    stats_cols = ['PTS', 'PLUS_MINUS', 'FG_PCT']
    for col in stats_cols:
        all_games[f'L5_{col}'] = all_games.groupby('TEAM_ID')[col].transform(lambda x: x.shift(1).rolling(5).mean())

    all_games['DAYS_REST'] = all_games.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days
    all_games['B2B'] = (all_games['DAYS_REST'] == 1).astype(int)
    
    train_df = all_games.dropna(subset=['L5_PTS', 'L10_WIN_RATE']).copy()
    features = [f'L5_{c}' for c in stats_cols] + ['B2B', 'IS_HOME', 'L10_WIN_RATE']
    
    clf_model = xgb.XGBClassifier(n_estimators=100, max_depth=3, learning_rate=0.1)
    clf_model.fit(train_df[features], train_df['WIN_BIN'])
    reg_model = xgb.XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.1)
    reg_model.fit(train_df[features], train_df['PLUS_MINUS'])

    p_stats_raw = leaguedashplayerstats.LeagueDashPlayerStats(season=season, per_mode_detailed='PerGame').get_data_frames()[0]
    player_stats = p_stats_raw[['PLAYER_NAME', 'TEAM_ID', 'PTS', 'REB', 'AST']]

    return clf_model, reg_model, all_games, player_stats, features

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

# --- 2. 預測與分析邏輯 (核心修正區) ---
def run_prediction(games, clf_model, reg_model, all_games_raw, player_stats, features_list):
    now_tw = datetime.now(tw_tz)
    results = {}
    for g in games:
        h_abbr, a_abbr = g['HOME_ABBR'], g['AWAY_ABBR']
        h_id, a_id = g['HOME_TEAM_ID'], g['VISITOR_TEAM_ID']
        
        h_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == h_abbr].tail(1)
        a_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == a_abbr].tail(1)
        
        h_in = h_feat[features_list].copy(); h_in['IS_HOME'] = 1
        a_in = a_feat[features_list].copy(); a_in['IS_HOME'] = 0
        
        # 模型輸出
        h_prob_raw = float(clf_model.predict_proba(h_in)[:, 1][0])
        a_prob_raw = float(clf_model.predict_proba(a_in)[:, 1][0])
        h_prob = (h_prob_raw / (h_prob_raw + a_prob_raw)) * 100
        a_prob = 100 - h_prob
        
        h_spread = float(reg_model.predict(h_in)[0])
        a_spread = float(reg_model.predict(a_in)[0])
        diff = h_spread - a_spread  # 正數代表主贏，負數代表客贏
        
        # 決定預測勝方
        pred_winner_abbr = h_abbr if diff > 0 else a_abbr
        pred_winner_name = TEAM_NAME_CH.get(pred_winner_abbr)
        
        # 建立一致性的理由
        reasons = []
        winner_feat = h_feat if diff > 0 else a_feat
        loser_feat = a_feat if diff > 0 else h_feat
        
        if winner_feat['L10_WIN_RATE'].values[0] > loser_feat['L10_WIN_RATE'].values[0]:
            reasons.append(f"📈 戰績優勢：{pred_winner_name} 的近十場表現更穩定。")
        if winner_feat['L5_PTS'].values[0] > loser_feat['L5_PTS'].values[0]:
            reasons.append(f"🔥 火力壓制：{pred_winner_name} 近期場均得分高於對手。")
        if diff > 0:
            reasons.append(f"🏠 地利之便：{pred_winner_name} 坐鎮主場具備心理優勢。")
        if loser_feat['B2B'].values[0] == 1:
            reasons.append(f"🔋 體能落差：對手目前處於背靠背(B2B)作戰，體力堪憂。")

        results[str(g['GAME_ID'])] = {
            'h_prob': h_prob, 'a_prob': a_prob,
            'diff': round(diff, 1),
            'winner_abbr': pred_winner_abbr,
            'reasons': reasons,
            'lock_time': now_tw.strftime('%Y-%m-%d %H:%M:%S')
        }
    return results

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
        snapshot_file = get_snapshot_path(date_key)
        
        if not games:
            st.info(f"📅 {date_key} 暫無賽程")
            continue

        is_locked = os.path.exists(snapshot_file)
        if is_locked:
            with open(snapshot_file, 'r', encoding='utf-8') as f: locked_data = json.load(f)
            st.success(f"🔒 數據已封盤 ({locked_data[list(locked_data.keys())[0]].get('lock_time', 'N/A')})")
        else:
            locked_data = run_prediction(games, clf_model, reg_model, all_games_raw, all_player_stats, features_list)
            st.warning("⏳ 即時預測模式")

        # 場次選擇
        options = [f"{TEAM_NAME_CH.get(g['AWAY_ABBR'])} @ {TEAM_NAME_CH.get(g['HOME_ABBR'])}" for g in games]
        sel_idx = st.selectbox("🎯 選擇場次", range(len(options)), key=f"sel_{date_key}")
        
        g_data = games[sel_idx]
        res = locked_data.get(str(g_data['GAME_ID']))
        
        if res:
            # 數據儀表板
            c1, c2, c3 = st.columns(3)
            h_name, a_name = TEAM_NAME_CH.get(g_data['HOME_ABBR']), TEAM_NAME_CH.get(g_data['AWAY_ABBR'])
            c1.metric(f"{h_name} 勝率", f"{res['h_prob']:.1f}%")
            c2.metric(f"{a_name} 勝率", f"{res['a_prob']:.1f}%")
            
            winner_name = TEAM_NAME_CH.get(res['winner_abbr'])
            score_text = f"{winner_name} 贏 {abs(res['diff'])} 分"
            c3.metric("預計分差", score_text, delta="模型預測方")

            # 分析建議 (修正後)
            st.write("### 💡 專家深度分析")
            advice_color = "success" if abs(res['diff']) > 5 else "info"
            st.markdown(f"**建議：{winner_name} {'強勢' if abs(res['diff']) > 7 else ''}看好獲勝**")
            
            for r in res['reasons']:
                st.write(f"- {r}")

            # 鎖定按鈕
            if not is_locked and st.button("🔒 鎖定今日數據", key=f"btn_{date_key}"):
                with open(snapshot_file, 'w', encoding='utf-8') as f: json.dump(locked_data, f, ensure_ascii=False)
                st.rerun()
            elif is_locked and st.button("🔓 解鎖數據", key=f"un_{date_key}"):
                os.remove(snapshot_file); st.rerun()

            # 對戰紀錄
            st.write("#### ⚔️ 本季對戰紀錄 (H2H)")
            h_id, a_abbr = g_data['HOME_TEAM_ID'], g_data['AWAY_ABBR']
            h2h = all_games_raw[((all_games_raw['TEAM_ID'] == h_id) & (all_games_raw['MATCHUP'].str.contains(a_abbr)))]
            if not h2h.empty:
                display_h2h = h2h[['GAME_DATE', 'MATCHUP', 'WL', 'SCORE_DISPLAY', 'PLUS_MINUS']].copy()
                display_h2h['GAME_DATE'] = display_h2h['GAME_DATE'].dt.strftime('%Y-%m-%d')
                display_h2h.columns = ['日期', '組合', '結果', '比分(主-客)', '分差']
                st.table(display_h2h.head(5))
