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

st.set_page_config(page_title="NBA 數據專家 v6.6", layout="wide")
st.title("🏀 NBA 數據專家 v6.6 (基礎 + 追蹤 + 戰術 全整合版)")

# --- 2. 核心穩定抓取函數 ---
def fetch_safe_df(endpoint_class, **kwargs):
    try:
        instance = endpoint_class(**kwargs)
        raw = instance.get_dict()
        res = raw['resultSets'][0] if 'resultSets' in raw else raw['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

@st.cache_data(ttl=3600)
def load_all_data_v66():
    nba_ids = [t['id'] for t in teams.get_teams()]
    
    # [A] V2 基礎數據：場均、進階
    df_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season='2025-26', measure_type_detailed_defense='Base')
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season='2025-26', measure_type_detailed_defense='Advanced')
    
    # [B] V2 追蹤數據：撥球、跑動、傳球
    df_hustle = fetch_safe_df(leaguehustlestatsteam.LeagueHustleStatsTeam, season='2025-26')
    df_track_spd = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season='2025-26', pt_measure_type='SpeedDistance')
    df_track_pass = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season='2025-26', pt_measure_type='Passing')
    
    # [C] V3 戰術數據：快攻 PPP、護框 D-FG%
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season='2025-26')
    df_rim = fetch_safe_df(leaguedashptdefend.LeagueDashPtDefend, season='2025-26', defense_category='Less Than 6 Ft')

    # 建立映射字典
    def to_map(df, cols): return df.set_index('TEAM_ID')[cols].to_dict('index') if not df.empty else {}
    
    base_map = to_map(df_base, ['PTS', 'REB', 'AST', 'FG_PCT'])
    adv_map = to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE'])
    hustle_map = to_map(df_hustle, ['DEFLECTIONS', 'CONTESTED_SHOTS'])
    spd_map = to_map(df_track_spd, ['DIST_MILES', 'AVG_SPEED'])
    pass_map = to_map(df_track_pass, ['PASSES_MADE'])
    trans_map = to_map(df_trans, ['PPP'])
    rim_map = to_map(df_rim, ['D_FG_PCT'])

    # [D] 歷史戰績與模型訓練 (包含全部維度)
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable='2025-26')
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    
    # 注入預測因子
    gf['T_ORTG'] = gf['TEAM_ID'].map(lambda x: adv_map.get(x, {}).get('OFF_RATING', 110))
    gf['T_DRTG'] = gf['TEAM_ID'].map(lambda x: adv_map.get(x, {}).get('DEF_RATING', 110))
    gf['T_TRANS'] = gf['TEAM_ID'].map(lambda x: trans_map.get(x, {}).get('PPP', 1.1))
    gf['T_RIM'] = gf['TEAM_ID'].map(lambda x: rim_map.get(x, {}).get('D_FG_PCT', 0.6))
    gf['L10_W'] = gf.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())

    feats = ['IS_HOME', 'REST_DAYS', 'T_ORTG', 'T_DRTG', 'T_TRANS', 'T_RIM', 'L10_W']
    train = gf.fillna(0)
    clf = xgb.XGBClassifier().fit(train[feats], train['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(train[feats], train['PLUS_MINUS'])
    
    # 球員數據 (傳統為主)
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season='2025-26', per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season='2025-26', per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST']], ps_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID')
    
    all_maps = {
        'base': base_map, 'adv': adv_map, 'hustle': hustle_map, 
        'spd': spd_map, 'pass': pass_map, 'trans': trans_map, 'rim': rim_map
    }
    return clf, reg, gf, ps_full, feats, all_maps, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, ps_full, feats, maps, last_update = load_all_data_v66()

# --- 3. 介面顯示 ---
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 本日無賽程。")
        else:
            id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
            games = {}
            for _, row in sb.iterrows():
                h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
                h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
                if h_abbr and a_abbr:
                    h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
                    if not h_last.empty:
                        prob = clf.predict_proba(h_last[feats])[0][1] * 100
                        diff = round(abs(float(reg.predict(h_last[feats])[0])))
                        games[f"{TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}"] = {
                            'h_id': h_id, 'a_id': a_id, 'h_name': TEAM_NAME_CH.get(h_abbr), 'a_name': TEAM_NAME_CH.get(a_abbr),
                            'prob': prob, 'diff': diff, 'winner': TEAM_NAME_CH.get(h_abbr if prob > 50 else a_abbr)
                        }

            if games:
                selected = st.selectbox("🎯 選擇分析場次", list(games.keys()), key=f"s_{i}")
                res = games[selected]
                
                # A. 預測摘要卡片
                st.markdown(f"### 🏟️ {selected}")
                c1, c2, c3 = st.columns(3)
                c1.metric(res['h_name'], f"{res['prob']:.1f}%")
                c2.metric(res['a_name'], f"{100-res['prob']:.1f}%")
                c3.metric("勝率預測贏家", res['winner'], f"預計分差 {res['diff']}")

                # 幫助函數
                def get_m(m, tid, k): return maps[m].get(tid, {}).get(k, 0)

                st.markdown("---")
                # B. 表格 1：團隊基礎實力 (V2 核心)
                st.markdown("##### 📊 團隊基礎數據 (Base Stats)")
                base_df = pd.DataFrame({
                    "指標": ["場均得分", "場均籃板", "場均助攻", "團隊命中率", "進攻效率 (OffRtg)", "防守效率 (DefRtg)"],
                    res['h_name']: [get_m('base', res['h_id'], 'PTS'), get_m('base', res['h_id'], 'REB'), get_m('base', res['h_id'], 'AST'), f"{get_m('base', res['h_id'], 'FG_PCT'):.1%}", get_m('adv', res['h_id'], 'OFF_RATING'), get_m('adv', res['h_id'], 'DEF_RATING')],
                    res['a_name']: [get_m('base', res['a_id'], 'PTS'), get_m('base', res['a_id'], 'REB'), get_m('base', res['a_id'], 'AST'), f"{get_m('base', res['a_id'], 'FG_PCT'):.1%}", get_m('adv', res['a_id'], 'OFF_RATING'), get_m('adv', res['a_id'], 'DEF_RATING')]
                })
                st.table(base_df)

                # C. 表格 2：體能與防守積極度 (V2 追蹤數據)
                st.markdown("##### 🏃‍♂️ 體能與積極度追蹤 (Hustle & Tracking)")
                track_df = pd.DataFrame({
                    "追蹤指標": ["撥球破壞 (Deflections)", "干擾投籃 (Contested)", "場均跑動里程 (Miles)", "平均速度 (MPH)", "場均傳球數"],
                    res['h_name']: [get_m('hustle', res['h_id'], 'DEFLECTIONS'), get_m('hustle', res['h_id'], 'CONTESTED_SHOTS'), get_m('spd', res['h_id'], 'DIST_MILES'), get_m('spd', res['h_id'], 'AVG_SPEED'), get_m('pass', res['h_id'], 'PASSES_MADE')],
                    res['a_name']: [get_m('hustle', res['a_id'], 'DEFLECTIONS'), get_m('hustle', res['a_id'], 'CONTESTED_SHOTS'), get_m('spd', res['a_id'], 'DIST_MILES'), get_m('spd', res['a_id'], 'AVG_SPEED'), get_m('pass', res['a_id'], 'PASSES_MADE')]
                })
                st.table(track_df)

                # D. 表格 3：戰術剋制關係 (V3 戰術數據)
                st.markdown("##### ⚔️ 戰術剋制與護框效率 (Tactical Matchup)")
                tactical_df = pd.DataFrame({
                    "戰術指標": ["轉換進攻效率 (PPP)", "籃框護框命中率 (D-FG%)"],
                    res['h_name']: [get_m('trans', res['h_id'], 'PPP'), f"{get_m('rim', res['h_id'], 'D_FG_PCT'):.1%}"],
                    res['a_name']: [get_m('trans', res['a_id'], 'PPP'), f"{get_m('rim', res['a_id'], 'D_FG_PCT'):.1%}"]
                })
                st.table(tactical_df)

                # E. 球員數據
                st.markdown("##### 🚀 核心球員傳統數據 (Top 6)")
                def show_p(tid, name):
                    st.write(f"**{name}**")
                    p_df = ps_full[ps_full['TEAM_ID'] == tid].sort_values('PTS', ascending=False).head(6)
                    p_df = p_df[['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT', 'PIE']]
                    p_df.columns = ['球員', '得分', '籃板', '助攻', '真實命中%', '貢獻度(PIE)']
                    st.dataframe(p_df.style.format({'得分':'{:.1f}', '籃板':'{:.1f}', '助攻':'{:.1f}', '真實命中%':'{:.1%}', '貢獻度(PIE)':'{:.1%}'}), hide_index=True, use_container_width=True)

                show_p(res['h_id'], f"🏠 {res['h_name']}")
                show_p(res['a_id'], f"✈️ {res['a_name']}")

st.sidebar.caption(f"🕒 更新時間：{last_update}")
