import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    synergyplaytypes, leaguedashptdefend
)
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import pytz, warnings, time
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

st.set_page_config(page_title="NBA 數據專家 v6.4", layout="wide")
st.title("🏀 NBA 數據專家 v6.4 (終極穩定版)")

# --- 2. 核心數據處理 (徹底解決 KeyError) ---

def fetch_nba_data(endpoint_class, **kwargs):
    """
    底層抓取函數，手動解析 JSON 以避免 nba_api 內部的 KeyError
    """
    try:
        # 實例化但不立即調用 get_data_frames
        instance = endpoint_class(**kwargs)
        raw_data = instance.get_dict()
        
        # 關鍵：同時檢查兩種可能的鍵值
        if 'resultSets' in raw_data:
            res = raw_data['resultSets'][0]
        elif 'resultSet' in raw_data:
            res = raw_data['resultSet']
        else:
            # 針對某些特殊的 Synergy 資料格式
            return instance.get_data_frames()[0]
            
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except Exception as e:
        st.sidebar.warning(f"數據源暫時離線: {endpoint_class.__name__}")
        return pd.DataFrame()

@st.cache_data(ttl=3600)
def load_all_data_v64():
    nba_ids = [t['id'] for t in teams.get_teams()]
    
    # [1] 團隊進階與四大因素 (採用新抓取邏輯)
    df_adv = fetch_nba_data(leaguedashteamstats.LeagueDashTeamStats, season='2025-26', measure_type_detailed_defense='Advanced')
    df_ff = fetch_nba_data(leaguedashteamstats.LeagueDashTeamStats, season='2025-26', measure_type_detailed_defense='FourFactors')
    
    # [2] 拚勁與追蹤數據 (v6.2)
    df_hustle = fetch_nba_data(leaguehustlestatsteam.LeagueHustleStatsTeam, season='2025-26')
    df_track_spd = fetch_nba_data(leaguedashptstats.LeagueDashPtStats, season='2025-26', pt_measure_type='SpeedDistance')
    df_track_pass = fetch_nba_data(leaguedashptstats.LeagueDashPtStats, season='2025-26', pt_measure_type='Passing')

    # [3] 戰術與護框數據 (v6.3)
    df_trans = fetch_nba_data(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season='2025-26')
    df_iso = fetch_nba_data(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Isolation', player_or_team_abbreviation='T', season='2025-26')
    df_rim = fetch_nba_data(leaguedashptdefend.LeagueDashPtDefend, season='2025-26', defense_category='Less Than 6 Ft')

    # --- 建立映射字典 ---
    def get_map(df, key_col, val_cols):
        return df.set_index(key_col)[val_cols].to_dict('index') if not df.empty else {}

    adv_map = get_map(df_adv, 'TEAM_ID', ['OFF_RATING', 'DEF_RATING', 'PACE', 'TS_PCT'])
    ff_map = get_map(df_ff, 'TEAM_ID', ['EFG_PCT', 'TOV_PCT'])
    hustle_map = get_map(df_hustle, 'TEAM_ID', ['DEFLECTIONS', 'CONTESTED_SHOTS'])
    spd_map = get_map(df_track_spd, 'TEAM_ID', ['DIST_MILES', 'AVG_SPEED'])
    pass_map = get_map(df_track_pass, 'TEAM_ID', ['PASSES_MADE'])
    trans_map = get_map(df_trans, 'TEAM_ID', ['PPP'])
    iso_map = get_map(df_iso, 'TEAM_ID', ['PPP'])
    rim_map = get_map(df_rim, 'TEAM_ID', ['D_FG_PCT'])

    # [4] 歷史戰績整合 (用於訓練模型)
    gf_raw = fetch_nba_data(leaguegamefinder.LeagueGameFinder, season_nullable='2025-26')
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    
    # 注入全部特徵
    def map_v(tid, d, k, default): return d.get(tid, {}).get(k, default)

    gf['T_ORTG'] = gf['TEAM_ID'].apply(lambda x: map_v(x, adv_map, 'OFF_RATING', 110))
    gf['T_DRTG'] = gf['TEAM_ID'].apply(lambda x: map_v(x, adv_map, 'DEF_RATING', 110))
    gf['T_EFG'] = gf['TEAM_ID'].apply(lambda x: map_v(x, ff_map, 'EFG_PCT', 0.52))
    gf['T_PASS'] = gf['TEAM_ID'].apply(lambda x: map_v(x, pass_map, 'PASSES_MADE', 280))
    gf['T_TRANS'] = gf['TEAM_ID'].apply(lambda x: map_v(x, trans_map, 'PPP', 1.1))
    gf['T_RIM'] = gf['TEAM_ID'].apply(lambda x: map_v(x, rim_map, 'D_FG_PCT', 0.62))
    gf['L10_W'] = gf.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())

    feats = ['IS_HOME', 'REST_DAYS', 'T_ORTG', 'T_DRTG', 'T_EFG', 'L10_W', 'T_PASS', 'T_TRANS', 'T_RIM']
    train = gf.fillna(0)
    clf = xgb.XGBClassifier().fit(train[feats], train['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(train[feats], train['PLUS_MINUS'])
    
    # 球員數據 (採用新邏輯抓取)
    ps_base = fetch_nba_data(leaguedashplayerstats.LeagueDashPlayerStats, season='2025-26', per_mode_detailed='PerGame')
    ps_adv = fetch_nba_data(leaguedashplayerstats.LeagueDashPlayerStats, season='2025-26', per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_base[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS']], ps_adv[['PLAYER_ID', 'TS_PCT', 'USG_PCT', 'PIE']], on='PLAYER_ID')
    
    full_maps = {'adv': adv_map, 'ff': ff_map, 'hustle': hustle_map, 'spd': spd_map, 'pass': pass_map, 'trans': trans_map, 'iso': iso_map, 'rim': rim_map}
    
    return clf, reg, gf, ps_full, feats, full_maps, datetime.now(tw_tz).strftime("%H:%M")

# 初始化數據
try:
    clf, reg, gf, ps_full, feats, full_maps, last_update = load_all_data_v64()
except:
    st.error("NBA 官網數據連線超時，請稍後重新整理頁面。")
    st.stop()

# --- 3. 介面顯示 ---
lock_prob = st.sidebar.checkbox("🔒 鎖定預測勝率", value=False)
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        current_date = dates[i]
        sb = fetch_nba_data(scoreboardv2.ScoreboardV2, game_date=current_date.strftime('%m/%d/%Y'))
        
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
                
                # [A] 預測卡片
                st.markdown(f"#### 🏟️ {selected}")
                c1, c2, c3 = st.columns(3)
                c1.metric(res['h_name'], f"{res['prob']:.1f}%")
                c2.metric(res['a_name'], f"{100 - res['prob']:.1f}%")
                c3.metric("預測贏家", res['winner'], f"預計分差: {res['diff']}")

                # [B] 戰術與對位數據 (中文標題)
                st.markdown("---")
                st.markdown("##### ⚔️ 戰術體系與護框效率 (Tactical Matchup)")
                def get_m(m, tid, k): return full_maps[m].get(tid, {}).get(k, 0)
                
                tactical_df = pd.DataFrame({
                    "數據指標": ["轉換進攻得分效率 (PPP)", "單打得分效率 (PPP)", "護框防守命中率 (D-FG%)", "場均傳球次數", "防守撥球 (Deflections)"],
                    res['h_name']: [get_m('trans', res['h_id'], 'PPP'), get_m('iso', res['h_id'], 'PPP'), f"{get_m('rim', res['h_id'], 'D_FG_PCT'):.1%}", get_m('pass', res['h_id'], 'PASSES_MADE'), get_m('hustle', res['h_id'], 'DEFLECTIONS')],
                    res['a_name']: [get_m('trans', res['a_id'], 'PPP'), get_m('iso', res['a_id'], 'PPP'), f"{get_m('rim', res['a_id'], 'D_FG_PCT'):.1%}", get_m('pass', res['a_id'], 'PASSES_MADE'), get_m('hustle', res['a_id'], 'DEFLECTIONS')]
                })
                st.table(tactical_df)

                # [C] 本季對戰紀錄 (中文標題)
                st.markdown("##### ⚔️ 本季對戰歷史")
                h2h = gf[(gf['TEAM_ABBREVIATION'] == res['h_abbr']) & (gf['MATCHUP'].str.contains(res['a_abbr']))].sort_values('GAME_DATE', ascending=False)
                if not h2h.empty:
                    h2h['結果'] = h2h.apply(lambda r: f"W ({r.PTS}-{int(r.PTS-r.PLUS_MINUS)})" if r.WL == 'W' else f"L ({r.PTS}-{int(r.PTS-r.PLUS_MINUS)})", axis=1)
                    h2h_display = h2h[['GAME_DATE', 'MATCHUP', '結果']].rename(columns={'GAME_DATE': '比賽日期', 'MATCHUP': '對戰組合', '結果': '賽果'})
                    h2h_display['比賽日期'] = h2h_display['比賽日期'].dt.strftime('%Y-%m-%d')
                    st.dataframe(h2h_display, hide_index=True, use_container_width=True)
                else: st.write("本季尚未交手。")

                # [D] 核心球員數據 (中文標題、寬版分列)
                st.markdown("##### 🚀 核心球員進階數據 (Top 6)")
                def draw_player_table(tid, title):
                    st.subheader(title)
                    df = ps_full[ps_full['TEAM_ID'] == tid].sort_values('PTS', ascending=False).head(6)
                    df = df[['PLAYER_NAME', 'PTS', 'TS_PCT', 'USG_PCT', 'PIE']]
                    df.columns = ['姓名', '場均得分', '真實命中%', '使用率%', '貢獻值(PIE)']
                    st.dataframe(df.style.format({'真實命中%':'{:.1%}', '使用率%':'{:.1%}', '貢獻值(PIE)':'{:.1%}'}), hide_index=True, use_container_width=True)

                draw_player_table(res['h_id'], f"🏠 {res['h_name']}")
                draw_player_table(res['a_id'], f"✈️ {res['a_name']}")

st.sidebar.caption(f"🕒 數據最後更新：{last_update}")
