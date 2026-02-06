import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    synergyplaytypes, leaguedashptdefend
)
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import pytz, warnings
from datetime import datetime, timedelta

# --- 1. 基本設定 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')

TEAM_NAME_CH = {
    'ATL': '亞特蘭大老鷹', 'BKN': '布魯克林籃網', 'BOS': '波士頓塞爾提克',
    'CHA': '夏洛特黃蜂', 'CHI': '芝加哥公牛', 'CLE': '克里夫蘭騎士',
    'DAL': '達拉斯獨行俠', 'DEN': '丹佛金塊', 'DET': '底特律活塞',
    'GSW': '金州勇士', 'HOU': '休士頓火箭', 'IND': '印第安納溜馬',
    'LAC': '洛杉磯快艇', 'LAL': '洛杉磯湖人', 'MEM': '曼非斯灰熊',
    'MIA': '邁阿密熱火', 'MIL': '密爾瓦基公鹿', 'MIN': '明尼蘇達灰狼',
    'NOP': '紐奧良鵜鶘', 'NYK': '紐約尼克', 'OKC': '奧克拉荷馬雷霆',
    'ORL': '奧蘭多魔術', 'PHI': '費城 76 人', 'PHX': '鳳凰城太陽',
    'POR': '波特蘭開拓者', 'SAC': '沙加邁度國王', 'SAS': '聖安東尼奧馬刺',
    'TOR': '多倫多暴龍', 'UTA': '猶他爵士', 'WAS': '華盛頓巫師'
}

st.set_page_config(page_title="NBA 數據專家 v6.8", layout="wide")
st.title("🏀 NBA 數據專家 v6.8 (場均修正 + 智能建模版)")

# --- 2. 穩定抓取函數 ---
def fetch_safe_df(endpoint_class, **kwargs):
    try:
        instance = endpoint_class(**kwargs)
        raw = instance.get_dict()
        res = raw['resultSets'][0] if 'resultSets' in raw else raw['resultSet']
        df = pd.DataFrame(res['rowSet'], columns=res['headers'])
        # 統一將 TEAM_ID 轉為 int 以便匹配
        if 'TEAM_ID' in df.columns: df['TEAM_ID'] = df['TEAM_ID'].astype(int)
        return df
    except: return pd.DataFrame()

@st.cache_data(ttl=3600)
def load_all_data_v68():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S = '2025-26'
    
    # [A] 團隊基礎與進階 (強制 PerGame)
    df_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    
    # [B] 體能追蹤 (強制 PerGame)
    df_hustle = fetch_safe_df(leaguehustlestatsteam.LeagueHustleStatsTeam, season=S, per_mode_time='PerGame')
    df_track_spd = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
    df_track_pass = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='Passing', per_mode_simple='PerGame')
    
    # [C] 戰術與護框 (修正 0 值問題)
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S)
    df_rim = fetch_safe_df(leaguedashptdefend.LeagueDashPtDefend, season=S, defense_category='Less Than 6 Ft')

    def to_map(df, cols): 
        if df.empty: return {}
        # 處理 Synergy 數據欄位名稱不一致
        id_col = 'TEAM_ID' if 'TEAM_ID' in df.columns else 'ID'
        return df.set_index(id_col)[cols].to_dict('index')
    
    maps = {
        'base': to_map(df_base, ['PTS', 'REB', 'AST', 'FG_PCT']),
        'adv': to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE']),
        'hustle': to_map(df_hustle, ['DEFLECTIONS', 'CONTESTED_SHOTS']),
        'spd': to_map(df_track_spd, ['DIST_MILES', 'AVG_SPEED']),
        'pass': to_map(df_track_pass, ['PASSES_MADE']),
        'trans': to_map(df_trans, ['PPP']),
        'rim': to_map(df_rim, ['D_FG_PCT'])
    }

    # [D] 模型訓練：將所有微觀數據納入特徵
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    
    # 注入全部特徵作為預測依據
    def get_v(tid, m, k, default=0): return maps[m].get(int(tid), {}).get(k, default)

    gf['T_ORTG'] = gf['TEAM_ID'].apply(lambda x: get_v(x, 'adv', 'OFF_RATING', 110))
    gf['T_DRTG'] = gf['TEAM_ID'].apply(lambda x: get_v(x, 'adv', 'DEF_RATING', 110))
    gf['T_DEFL'] = gf['TEAM_ID'].apply(lambda x: get_v(x, 'hustle', 'DEFLECTIONS', 14))
    gf['T_PASS'] = gf['TEAM_ID'].apply(lambda x: get_v(x, 'pass', 'PASSES_MADE', 280))
    gf['T_TRANS'] = gf['TEAM_ID'].apply(lambda x: get_v(x, 'trans', 'PPP', 1.1))
    gf['T_RIM'] = gf['TEAM_ID'].apply(lambda x: get_v(x, 'rim', 'D_FG_PCT', 0.6))
    
    # 這裡就是您問的：全部加進模型分析
    feats = ['IS_HOME', 'REST_DAYS', 'T_ORTG', 'T_DRTG', 'T_DEFL', 'T_PASS', 'T_TRANS', 'T_RIM']
    train_df = gf.fillna(0)
    clf = xgb.XGBClassifier().fit(train_df[feats], train_df['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(train_df[feats], train_df['PLUS_MINUS'])
    
    # 球員數據
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST']], ps_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID')
    
    return clf, reg, gf, ps_full, feats, maps, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, ps_full, feats, maps, last_update = load_all_data_v68()

# --- 3. 介面與表格顯示 ---
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 今日無賽程")
        else:
            id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
            games = {}
            for _, row in sb.iterrows():
                h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
                h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
                if h_abbr and a_abbr:
                    h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
                    if not h_last.empty:
                        # 使用全維度特徵進行預測
                        prob = clf.predict_proba(h_last[feats])[0][1] * 100
                        diff = round(abs(float(reg.predict(h_last[feats])[0])), 1)
                        games[f"{TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}"] = {
                            'h_id': h_id, 'a_id': a_id, 'h_name': TEAM_NAME_CH.get(h_abbr), 'a_name': TEAM_NAME_CH.get(a_abbr),
                            'prob': prob, 'diff': diff, 'winner': TEAM_NAME_CH.get(h_abbr if prob > 50 else a_abbr)
                        }

            if games:
                selected = st.selectbox("🎯 選擇場次", list(games.keys()), key=f"s_{i}")
                res = games[selected]
                st.markdown(f"### 🏟️ {selected}")
                c1, c2, c3 = st.columns(3)
                c1.metric(res['h_name'], f"{res['prob']:.1f}%")
                c2.metric(res['a_name'], f"{100-res['prob']:.1f}%")
                c3.metric("AI 預測贏家", res['winner'], f"分差預測: {res['diff']}")

                def get_m(m, tid, k): return maps[m].get(int(tid), {}).get(k, 0)

                # 表格 1：基礎數據 (場均修正)
                st.subheader("📊 1. 團隊場均基礎數據")
                st.table(pd.DataFrame({
                    "指標項目": ["場均得分", "場均籃板", "場均助攻", "團隊命中率", "進攻效率", "防守效率"],
                    res['h_name']: [get_m('base', res['h_id'], 'PTS'), get_m('base', res['h_id'], 'REB'), get_m('base', res['h_id'], 'AST'), f"{get_m('base', res['h_id'], 'FG_PCT'):.1%}", get_m('adv', res['h_id'], 'OFF_RATING'), get_m('adv', res['h_id'], 'DEF_RATING')],
                    res['a_name']: [get_m('base', res['a_id'], 'PTS'), get_m('base', res['a_id'], 'REB'), get_m('base', res['a_id'], 'AST'), f"{get_m('base', res['a_id'], 'FG_PCT'):.1%}", get_m('adv', res['a_id'], 'OFF_RATING'), get_m('adv', res['a_id'], 'DEF_RATING')]
                }))

                # 表格 2：體能追蹤 (場均修正)
                st.subheader("🏃‍♂️ 2. 體能與積極度 (場均)")
                st.table(pd.DataFrame({
                    "指標": ["撥球破壞 (Deflections)", "干擾投籃 (Contested)", "場均里程 (Miles)", "平均速度 (MPH)", "場均傳球數"],
                    res['h_name']: [round(get_m('hustle', res['h_id'], 'DEFLECTIONS'), 1), round(get_m('hustle', res['h_id'], 'CONTESTED_SHOTS'), 1), get_m('spd', res['h_id'], 'DIST_MILES'), get_m('spd', res['h_id'], 'AVG_SPEED'), round(get_m('pass', res['h_id'], 'PASSES_MADE'), 1)],
                    res['a_name']: [round(get_m('hustle', res['a_id'], 'DEFLECTIONS'), 1), round(get_m('hustle', res['a_id'], 'CONTESTED_SHOTS'), 1), get_m('spd', res['a_id'], 'DIST_MILES'), get_m('spd', res['a_id'], 'AVG_SPEED'), round(get_m('pass', res['a_id'], 'PASSES_MADE'), 1)]
                }))

                # 表格 3：戰術與護框 (修復 0 值)
                st.subheader("⚔️ 3. 戰術剋制與護框 (模型關鍵特徵)")
                st.table(pd.DataFrame({
                    "戰術指標": ["轉換進攻效率 (PPP)", "籃框護框命中率 (D-FG%)"],
                    res['h_name']: [get_m('trans', res['h_id'], 'PPP'), f"{get_m('rim', res['h_id'], 'D_FG_PCT'):.1%}"],
                    res['a_name']: [get_m('trans', res['a_id'], 'PPP'), f"{get_m('rim', res['a_id'], 'D_FG_PCT'):.1%}"]
                }))

                # 球員數據 (場均)
                st.subheader("🚀 4. 核心球員數據 (Top 6)")
                for tid, name in [(res['h_id'], f"🏠 {res['h_name']}"), (res['a_id'], f"✈️ {res['a_name']}")]:
                    st.write(f"**{name}**")
                    p_df = ps_full[ps_full['TEAM_ID'] == tid].sort_values('PTS', ascending=False).head(6)
                    st.dataframe(p_df[['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT']].rename(columns={'PLAYER_NAME':'球員','PTS':'得分','REB':'籃板','AST':'助攻','TS_PCT':'真實命中%'}), hide_index=True)

st.sidebar.caption(f"🕒 更新時間：{last_update}")
