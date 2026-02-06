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

st.set_page_config(page_title="NBA 2026 深度分析系統 v4.5", layout="wide")
st.title("🏀 NBA 終極預測系統")

def get_snapshot_path(date_key):
    return f"nba_snapshot_{date_key}.json"

# --- 1. 數據與模型 ---
@st.cache_data(ttl=600)
def get_comprehensive_data(season):
    all_games = pd.DataFrame()
    player_stats = pd.DataFrame()
    for i in range(3):
        try:
            gamefinder = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60)
            all_games = gamefinder.get_data_frames()[0]
            if not all_games.empty: break
        except: time.sleep(2)
    
    if all_games.empty: return None, None, pd.DataFrame(), pd.DataFrame(), []

    all_games['GAME_DATE'] = pd.to_datetime(all_games['GAME_DATE'])
    all_games = all_games.sort_values(['TEAM_ID', 'GAME_DATE'])
    all_games['IS_HOME'] = all_games['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    all_games['WIN_BIN'] = all_games['WL'].apply(lambda x: 1 if x == 'W' else 0)
    all_games['L10_WIN_RATE'] = all_games.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10).mean())
    
    stats_cols = ['PTS', 'PLUS_MINUS', 'FG_PCT']
    for col in stats_cols:
        all_games[f'L5_{col}'] = all_games.groupby('TEAM_ID')[col].transform(lambda x: x.shift(1).rolling(5).mean())

    all_games['B2B'] = (all_games.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days == 1).astype(int)
    
    train_df = all_games.dropna(subset=['L5_PTS', 'L10_WIN_RATE']).copy()
    features = [f'L5_{c}' for c in stats_cols] + ['B2B', 'IS_HOME', 'L10_WIN_RATE']
    
    clf = xgb.XGBClassifier(n_estimators=100, max_depth=3, learning_rate=0.1)
    clf.fit(train_df[features], train_df['WIN_BIN'])
    reg = xgb.XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.1)
    reg.fit(train_df[features], train_df['PLUS_MINUS'])

    try:
        p_stats = leaguedashplayerstats.LeagueDashPlayerStats(season=season, per_mode_detailed='PerGame').get_data_frames()[0]
        player_stats = p_stats[['PLAYER_NAME', 'TEAM_ID', 'PTS', 'REB', 'AST']]
    except: pass

    return clf, reg, all_games, player_stats, features

@st.cache_data(ttl=600)
def get_team_roster(team_id):
    try:
        roster = commonteamroster.CommonTeamRoster(team_id=team_id, timeout=30).get_data_frames()[0]
        if 'PLAYER' in roster.columns: roster = roster.rename(columns={'PLAYER': 'PLAYER_NAME'})
        return roster[['PLAYER_NAME']]
    except: return pd.DataFrame(columns=['PLAYER_NAME'])

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

# --- 2. 深度分析引擎 ---
def run_prediction(games, clf, reg, all_games_raw, player_stats, features_list):
    results = {}
    for g in games:
        h_id, a_id = g['HOME_TEAM_ID'], g['VISITOR_TEAM_ID']
        h_abbr, a_abbr = g['HOME_ABBR'], g['AWAY_ABBR']
        h_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == h_abbr].tail(1)
        a_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == a_abbr].tail(1)
        
        if h_feat.empty or a_feat.empty: continue

        h_in = h_feat[features_list].copy(); h_in['IS_HOME'] = 1
        a_in = a_feat[features_list].copy(); a_in['IS_HOME'] = 0
        
        h_p = (float(clf.predict_proba(h_in)[:, 1][0]) / (float(clf.predict_proba(h_in)[:, 1][0]) + float(clf.predict_proba(a_in)[:, 1][0]))) * 100
        diff = float(reg.predict(h_in)[0]) - float(reg.predict(a_in)[0])
        
        # 提取指標
        h_wr, a_wr = h_feat['L10_WIN_RATE'].values[0]*100, a_feat['L10_WIN_RATE'].values[0]*100
        h_pts, a_pts = h_feat['L5_PTS'].values[0], a_feat['L5_PTS'].values[0]
        h_b2b, a_b2b = h_feat['B2B'].values[0], a_feat['B2B'].values[0]

        # 1. 點列式指標
        h_idx = [f"🟢 近十場勝率: {h_wr:.0f}%", f"🟢 近五場得分: {h_pts:.1f}"]
        a_idx = [f"🔵 近十場勝率: {a_wr:.0f}%", f"🔵 近五場得分: {a_pts:.1f}"]
        if h_b2b: h_idx.append("🔴 警訊: 背靠背體能劣勢")
        if a_b2b: a_idx.append("🔴 警訊: 背靠背體能劣勢")

        # 2. 整合文字分析內容
        winner_name = TEAM_NAME_CH.get(h_abbr if diff > 0 else a_abbr)
        loser_name = TEAM_NAME_CH.get(a_abbr if diff > 0 else h_abbr)
        
        summary_text = f"本場比賽模型更看好 **{winner_name}**。分析其核心優勢在於"
        if (h_wr > a_wr and diff > 0) or (a_wr > h_wr and diff < 0):
            summary_text += "近期更為穩定的勝率表現，"
        if (h_pts > a_pts and diff > 0) or (a_pts > h_pts and diff < 0):
            summary_text += "以及更強大的火力輸出能力。"
        else:
            summary_text += "防守端的效率以及主場數據支撐。"
        
        if (h_b2b and diff < 0) or (a_b2b and diff > 0):
            summary_text += f" 反觀 **{loser_name}** 受限於背靠背(B2B)的賽程壓力，體能恐成隱憂。"
        
        summary_text += f" 預計最終 **{winner_name}** 將以約 {abs(round(diff,1))} 分的優勢取勝。"

        def get_roster_data(t_id):
            ros = get_team_roster(t_id)
            if ros.empty or player_stats.empty: return []
            m = ros.merge(player_stats, on='PLAYER_NAME', how='left').fillna(0)
            return m.sort_values(by='PTS', ascending=False).head(5).to_dict('records')

        results[str(g['GAME_ID'])] = {
            'h_prob': h_p, 'a_prob': 100 - h_p, 'diff': round(diff, 1),
            'winner_abbr': h_abbr if diff > 0 else a_abbr,
            'h_idx': h_idx, 'a_idx': a_idx,
            'summary_report': summary_text,
            'h_roster': get_roster_data(h_id), 'a_roster': get_roster_data(a_id)
        }
    return results

# --- 3. UI 渲染 ---
clf, reg, all_games_raw, player_stats, features = get_comprehensive_data('2025-26')
date_list = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in date_list])

for i, tab in enumerate(tabs):
    with tab:
        current_date = date_list[i]; date_key = current_date.strftime('%Y-%m-%d')
        games = get_schedule_for_date(current_date); snapshot_file = get_snapshot_path(date_key)
        if not games: st.info("暫無賽程"); continue

        is_locked = os.path.exists(snapshot_file)
        btn_col, info_col = st.columns([1, 4])
        if not is_locked:
            if btn_col.button("🔒 鎖定今日數據", key=f"lk_{date_key}"):
                ld = run_prediction(games, clf, reg, all_games_raw, player_stats, features)
                with open(snapshot_file, 'w', encoding='utf-8') as f: json.dump(ld, f, ensure_ascii=False)
                st.rerun()
            info_col.warning("⏳ 即時模式")
        else:
            if btn_col.button("解鎖即時更新", key=f"ul_{date_key}"):
                os.remove(snapshot_file); st.rerun()
            info_col.success("🔒 封盤模式")

        game_names = [f"{TEAM_NAME_CH.get(g['AWAY_ABBR'])} @ {TEAM_NAME_CH.get(g['HOME_ABBR'])}" for g in games]
        sel_name = st.selectbox("🎯 選擇對戰場次", options=game_names, key=f"sb_{date_key}")
        
        if is_locked:
            with open(snapshot_file, 'r', encoding='utf-8') as f: ds = json.load(f)
        else:
            ds = run_prediction(games, clf, reg, all_games_raw, player_stats, features)

        g_data = games[game_names.index(sel_name)]
        res = ds.get(str(g_data['GAME_ID']), {})
        
        if res:
            h_n, a_n = TEAM_NAME_CH.get(g_data['HOME_ABBR']), TEAM_NAME_CH.get(g_data['AWAY_ABBR'])
            st.markdown(f"## 🏟️ {a_n} (客) @ {h_n} (主)")
            
            c1, c2, c3 = st.columns(3)
            c1.metric(f"{h_n} 勝率", f"{res['h_prob']:.1f}%")
            c2.metric(f"{a_n} 勝率", f"{res['a_prob']:.1f}%")
            c3.metric("預測贏家", TEAM_NAME_CH.get(res['winner_abbr']), delta=f"贏 {abs(res['diff'])} 分")

            st.write("---")
            # 分析報告區 (新欄位)
            st.subheader("📝 深度戰力分析報告")
            st.info(res.get('summary_report', "暫無分析"))

            # 優劣勢對比 (保留原始資訊，修復 NULL)
            st.write("#### 📊 雙方指標對比")
            left, right = st.columns(2)
            with left:
                st.markdown(f"**🏠 {h_n}**")
                for item in res.get('h_idx', []): st.write(item)
            with right:
                st.markdown(f"**✈️ {a_n}**")
                for item in res.get('a_idx', []): st.write(item)

            st.write("---")
            st.subheader("👤 核心球員數據")
            def safe_df(data):
                if not data: return pd.DataFrame(columns=['姓名','得分','籃板','助攻'])
                df = pd.DataFrame(data)
                return df[['PLAYER_NAME','PTS','REB','AST']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分','REB':'籃板','AST':'助攻'})
            cl, cr = st.columns(2)
            cl.dataframe(safe_df(res.get('h_roster')), hide_index=True, use_container_width=True)
            cr.dataframe(safe_df(res.get('a_roster')), hide_index=True, use_container_width=True)
