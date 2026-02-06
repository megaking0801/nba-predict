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

st.set_page_config(page_title="NBA 戰術預測 v6.3", layout="wide")
st.title("🏀 NBA 數據專家 v6.3 (Tactical & Matchup)")

# --- 2. 核心數據處理 (v6.3 戰術擴充版) ---
def get_safe_df(endpoint_call):
    try:
        return endpoint_call.get_data_frames()[0]
    except:
        return pd.DataFrame()

@st.cache_data(ttl=3600)
def load_all_data_v63():
    nba_ids = [t['id'] for t in teams.get_teams()]
    
    # [1] 基礎與進階
    df_adv = get_safe_df(leaguedashteamstats.LeagueDashTeamStats(season='2025-26', measure_type_detailed_defense='Advanced'))
    df_ff = get_safe_df(leaguedashteamstats.LeagueDashTeamStats(season='2025-26', measure_type_detailed_defense='FourFactors'))
    
    # [2] 拚勁與追蹤 (v6.2 功能)
    df_hustle = get_safe_df(leaguehustlestatsteam.LeagueHustleStatsTeam(season='2025-26'))
    df_track_spd = get_safe_df(leaguedashptstats.LeagueDashPtStats(season='2025-26', pt_measure_type='SpeedDistance'))
    df_track_pass = get_safe_df(leaguedashptstats.LeagueDashPtStats(season='2025-26', pt_measure_type='Passing'))

    # [3] 戰術 Playtype & 護框 (v6.3 新增)
    # 轉換進攻效率
    df_trans = get_safe_df(synergyplaytypes.SynergyPlayTypes(play_type_nullable='Transition', player_or_team_abbreviation='T', season='2025-26'))
    # 單打效率
    df_iso = get_safe_df(synergyplaytypes.SynergyPlayTypes(play_type_nullable='Isolation', player_or_team_abbreviation='T', season='2025-26'))
    # 護框數據 (對手籃框下命中率)
    df_rim = get_safe_df(leaguedashptdefend.LeagueDashPtDefend(season='2025-26', defense_category='Less Than 6 Ft'))

    # 建立映射字典
    adv_map = df_adv.set_index('TEAM_ID')[['OFF_RATING', 'DEF_RATING', 'PACE', 'TS_PCT']].to_dict('index') if not df_adv.empty else {}
    ff_map = df_ff.set_index('TEAM_ID')[['EFG_PCT', 'TOV_PCT']].to_dict('index') if not df_ff.empty else {}
    hustle_map = df_hustle.set_index('TEAM_ID')[['DEFLECTIONS', 'CONTESTED_SHOTS']].to_dict('index') if not df_hustle.empty else {}
    spd_map = df_track_spd.set_index('TEAM_ID')[['DIST_MILES', 'AVG_SPEED']].to_dict('index') if not df_track_spd.empty else {}
    pass_map = df_track_pass.set_index('TEAM_ID')[['PASSES_MADE']].to_dict('index') if not df_track_pass.empty else {}
    
    # v6.3 新映射
    trans_map = df_trans.set_index('TEAM_ID')[['PPP']].rename(columns={'PPP': 'TRANS_PPP'}).to_dict('index') if not df_trans.empty else {}
    iso_map = df_iso.set_index('TEAM_ID')[['PPP']].rename(columns={'PPP': 'ISO_PPP'}).to_dict('index') if not df_iso.empty else {}
    rim_map = df_rim.set_index('TEAM_ID')[['D_FG_PCT']].to_dict('index') if not df_rim.empty else {}

    # 歷史與特徵整合
    gf_raw = leaguegamefinder.LeagueGameFinder(season_nullable='2025-26').get_data_frames()[0]
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    
    # 注入全部特徵 (含 v6.3 戰術數據)
    def map_stat(tid, map_dict, key, default):
        return map_dict.get(tid, {}).get(key, default)

    gf['T_ORTG'] = gf['TEAM_ID'].apply(lambda x: map_stat(x, adv_map, 'OFF_RATING', 110))
    gf['T_DRTG'] = gf['TEAM_ID'].apply(lambda x: map_stat(x, adv_map, 'DEF_RATING', 110))
    gf['T_EFG'] = gf['TEAM_ID'].apply(lambda x: map_stat(x, ff_map, 'EFG_PCT', 0.52))
    gf['T_PASS'] = gf['TEAM_ID'].apply(lambda x: map_stat(x, pass_map, 'PASSES_MADE', 280))
    # v6.3 特徵
    gf['T_TRANS_PPP'] = gf['TEAM_ID'].apply(lambda x: map_stat(x, trans_map, 'TRANS_PPP', 1.1))
    gf['T_ISO_PPP'] = gf['TEAM_ID'].apply(lambda x: map_stat(x, iso_map, 'ISO_PPP', 0.9))
    gf['T_RIM_DFG'] = gf['TEAM_ID'].apply(lambda x: map_stat(x, rim_map, 'D_FG_PCT', 0.62))

    gf['L10_W'] = gf.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())

    feats = ['IS_HOME', 'REST_DAYS', 'T_ORTG', 'T_DRTG', 'T_EFG', 'L10_W', 'T_PASS', 'T_TRANS_PPP', 'T_ISO_PPP', 'T_RIM_DFG']
    
    train = gf.fillna(0)
    clf = xgb.XGBClassifier().fit(train[feats], train['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(train[feats], train['PLUS_MINUS'])
    
    # 球員數據
    ps_raw_base = leaguedashplayerstats.LeagueDashPlayerStats(season='2025-26', per_mode_detailed='PerGame').get_data_frames()[0]
    ps_raw_adv = leaguedashplayerstats.LeagueDashPlayerStats(season='2025-26', per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced').get_data_frames()[0]
    ps_full = pd.merge(ps_raw_base[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS']], ps_raw_adv[['PLAYER_ID', 'TS_PCT', 'USG_PCT', 'PIE']], on='PLAYER_ID')
    
    full_maps = {'adv': adv_map, 'ff': ff_map, 'hustle': hustle_map, 'spd': spd_map, 'pass': pass_map, 'trans': trans_map, 'iso': iso_map, 'rim': rim_map}
    
    return clf, reg, gf, ps_full, feats, full_maps, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, ps_full, feats, full_maps, last_update = load_all_data_v63()

# --- 3. 介面顯示 ---
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        current_date = dates[i]
        sb = get_safe_df(scoreboardv2.ScoreboardV2(game_date=current_date.strftime('%m/%d/%Y')))
        
        if sb.empty:
            st.info(f"📅 {current_date.strftime('%Y-%m-%d')} 無賽程。")
        else:
            id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
            game_results = {}
            for _, row in sb.iterrows():
                h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
                h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
                if h_abbr and a_abbr:
                    h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
                    if not h_last.empty:
                        prob = clf.predict_proba(h_last[feats])[0][1] * 100
                        diff = round(abs(float(reg.predict(h_last[feats])[0])))
                        game_results[f"{TEAM_NAME_CH.get(a_abbr, a_abbr)} @ {TEAM_NAME_CH.get(h_abbr, h_abbr)}"] = {
                            'h_name': TEAM_NAME_CH.get(h_abbr, h_abbr), 'h_id': h_id, 'h_abbr': h_abbr,
                            'a_name': TEAM_NAME_CH.get(a_abbr, a_abbr), 'a_id': a_id, 'a_abbr': a_abbr,
                            'prob': prob, 'diff': diff, 'winner': TEAM_NAME_CH.get(h_abbr if prob > 50 else a_abbr)
                        }

            if game_results:
                selected = st.selectbox("🎯 選擇場次", list(game_results.keys()), key=f"sel_{i}")
                res = game_results[selected]
                
                # 1. 核心預測
                st.markdown(f"#### 🏟️ {selected}")
                c1, c2, c3 = st.columns(3)
                c1.metric(res['h_name'], f"{res['prob']:.1f}%")
                c2.metric(res['a_name'], f"{100 - res['prob']:.1f}%")
                c3.metric("模型贏家", res['winner'], f"分差 {res['diff']}")

                # 2. 戰術與對位數據 (v6.3 精華)
                st.markdown("---")
                st.markdown("##### ⚔️ 戰術體系與護框效率對比 (Tactical Matchup)")
                
                def get_m(m_name, tid, key): return full_maps[m_name].get(tid, {}).get(key, 0)

                matchup_data = {
                    "數據指標": ["轉換進攻得分效率 (TRANS_PPP)", "單打得分效率 (ISO_PPP)", "護框防守命中率 (RIM_DFG%)", "場均傳球次數", "防守撥球次數 (Deflections)"],
                    res['h_name']: [get_m('trans', res['h_id'], 'TRANS_PPP'), get_m('iso', res['h_id'], 'ISO_PPP'), f"{get_m('rim', res['h_id'], 'D_FG_PCT'):.1%}", get_m('pass', res['h_id'], 'PASSES_MADE'), get_m('hustle', res['h_id'], 'DEFLECTIONS')],
                    res['a_name']: [get_m('trans', res['a_id'], 'TRANS_PPP'), get_m('iso', res['a_id'], 'ISO_PPP'), f"{get_m('rim', res['a_id'], 'D_FG_PCT'):.1%}", get_m('pass', res['a_id'], 'PASSES_MADE'), get_m('hustle', res['a_id'], 'DEFLECTIONS')]
                }
                st.table(pd.DataFrame(matchup_data))
                st.caption("💡 *TRANS_PPP 越高代表快攻越穩；RIM_DFG% 越低代表內線護框越強。*")

                # 3. 球員數據 (寬版顯示)
                st.markdown("##### 🚀 核心球員進階數據")
                def show_roster(tid, name):
                    df = ps_full[ps_full['TEAM_ID'] == tid].sort_values('PTS', ascending=False).head(6)
                    df.columns = ['球員', 'TID', '姓名', '得分', '真實命中%', '使用率%', '貢獻值(PIE)']
                    st.subheader(name)
                    st.dataframe(df[['姓名', '得分', '真實命中%', '使用率%', '貢獻值(PIE)']].style.format({'真實命中%':'{:.1%}', '使用率%':'{:.1%}', '貢獻值(PIE)':'{:.1%}'}), hide_index=True, use_container_width=True)

                show_roster(res['h_id'], f"🏠 {res['h_name']}")
                show_roster(res['a_id'], f"✈️ {res['a_name']}")

st.sidebar.caption(f"🕒 更新時間：{last_update}")
