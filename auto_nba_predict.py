import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats
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

st.set_page_config(page_title="NBA 追蹤分析 v6.2", layout="wide")
st.title("🏀 NBA 數據專家 v6.2 (Tracking & Hustle)")

# --- 2. 核心數據處理 ---
def get_safe_team_stats(measure_type):
    try:
        raw = leaguedashteamstats.LeagueDashTeamStats(season='2025-26', measure_type_detailed_defense=measure_type)
        data = raw.get_dict()
        if 'resultSets' in data: results = data['resultSets'][0]
        else: results = data['resultSet']
        return pd.DataFrame(results['rowSet'], columns=results['headers'])
    except: return pd.DataFrame()

@st.cache_data(ttl=3600)
def load_all_data_v62():
    nba_ids = [t['id'] for t in teams.get_teams()]
    
    # 1. 基礎與進階數據
    df_adv = get_safe_team_stats('Advanced')
    df_ff = get_safe_team_stats('FourFactors')
    
    # 2. 抓取 Hustle (拚勁) 數據: 干擾投籃、撥球
    try:
        hustle = leaguehustlestatsteam.LeagueHustleStatsTeam(season='2025-26').get_data_frames()[0]
        hustle_map = hustle.set_index('TEAM_ID')[['CONTESTED_SHOTS', 'DEFLECTIONS']].to_dict('index')
    except: hustle_map = {}

    # 3. 抓取 Tracking (追蹤) 數據: 跑動與傳球
    try:
        # 跑動距離與速度
        track_spd = leaguedashptstats.LeagueDashPtStats(season='2025-26', pt_measure_type='SpeedDistance').get_data_frames()[0]
        # 傳球數據
        track_pass = leaguedashptstats.LeagueDashPtStats(season='2025-26', pt_measure_type='Passing').get_data_frames()[0]
        
        # 合併 Tracking 數據字典
        spd_map = track_spd.set_index('TEAM_ID')[['DIST_MILES', 'AVG_SPEED']].to_dict('index')
        pass_map = track_pass.set_index('TEAM_ID')[['PASSES_MADE']].to_dict('index')
    except: 
        spd_map, pass_map = {}, {}

    # 建立映射字典
    adv_map = df_adv.set_index('TEAM_ID')[['OFF_RATING', 'DEF_RATING', 'NET_RATING', 'PACE', 'TS_PCT']].to_dict('index') if not df_adv.empty else {}
    ff_map = df_ff.set_index('TEAM_ID')[['EFG_PCT', 'TOV_PCT', 'OREB_PCT', 'FTA_RATE']].to_dict('index') if not df_ff.empty else {}

    # 4. 歷史戰績整合
    gf_raw = leaguegamefinder.LeagueGameFinder(season_nullable='2025-26').get_data_frames()[0]
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    
    # 注入模型特徵 (包含新增的 Tracking 數據)
    gf['T_ORTG'] = gf['TEAM_ID'].map(lambda x: adv_map.get(x, {}).get('OFF_RATING', 110))
    gf['T_DRTG'] = gf['TEAM_ID'].map(lambda x: adv_map.get(x, {}).get('DEF_RATING', 110))
    gf['T_EFG'] = gf['TEAM_ID'].map(lambda x: ff_map.get(x, {}).get('EFG_PCT', 0.5))
    
    # 新增特徵注入
    gf['T_DIST'] = gf['TEAM_ID'].map(lambda x: spd_map.get(x, {}).get('DIST_MILES', 18.0)) # 場均跑動里程
    gf['T_SPD'] = gf['TEAM_ID'].map(lambda x: spd_map.get(x, {}).get('AVG_SPEED', 4.0))   # 平均速度
    gf['T_PASS'] = gf['TEAM_ID'].map(lambda x: pass_map.get(x, {}).get('PASSES_MADE', 280)) # 傳球數
    gf['T_DFL'] = gf['TEAM_ID'].map(lambda x: hustle_map.get(x, {}).get('DEFLECTIONS', 12)) # 撥球數
    gf['T_CON'] = gf['TEAM_ID'].map(lambda x: hustle_map.get(x, {}).get('CONTESTED_SHOTS', 50)) # 干擾投籃

    gf['L10_W'] = gf.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())

    # 更新特徵列表
    feats = ['IS_HOME', 'REST_DAYS', 'T_ORTG', 'T_DRTG', 'T_EFG', 'L10_W', 'T_DIST', 'T_SPD', 'T_PASS', 'T_DFL', 'T_CON']
    
    train = gf.fillna(0)
    clf = xgb.XGBClassifier().fit(train[feats], train['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(train[feats], train['PLUS_MINUS'])
    
    # 球員數據
    ps_raw_base = leaguedashplayerstats.LeagueDashPlayerStats(season='2025-26', per_mode_detailed='PerGame').get_data_frames()[0]
    ps_raw_adv = leaguedashplayerstats.LeagueDashPlayerStats(season='2025-26', per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced').get_data_frames()[0]
    ps_full = pd.merge(
        ps_raw_base[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST']],
        ps_raw_adv[['PLAYER_ID', 'TS_PCT', 'EFG_PCT', 'USG_PCT', 'E_NET_RATING', 'PIE']],
        on='PLAYER_ID', how='inner'
    )
    
    # 打包所有數據字典供前端顯示
    full_maps = {
        'adv': adv_map, 'ff': ff_map, 'spd': spd_map, 
        'pass': pass_map, 'hustle': hustle_map
    }
    
    return clf, reg, gf, ps_full, feats, full_maps, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, ps_full, feats, full_maps, last_update = load_all_data_v62()

# --- 3. 介面與顯示 ---
col_t, col_l = st.columns([3, 1])
with col_l:
    lock_prob = st.checkbox("🔒 鎖定預測勝率", value=False)

dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        current_date = dates[i]
        try:
            sb = scoreboardv2.ScoreboardV2(game_date=current_date.strftime('%m/%d/%Y')).get_data_frames()[0]
        except: sb = pd.DataFrame()

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
                        h_l5 = "".join(gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(5)['WL'].tolist())
                        a_l5 = "".join(gf[gf['TEAM_ABBREVIATION'] == a_abbr].tail(5)['WL'].tolist())
                        
                        label = f"{TEAM_NAME_CH.get(a_abbr, a_abbr)} @ {TEAM_NAME_CH.get(h_abbr, h_abbr)}"
                        game_results[label] = {
                            'h_name': TEAM_NAME_CH.get(h_abbr, h_abbr), 'h_id': h_id, 'h_abbr': h_abbr, 'h_l5': h_l5,
                            'a_name': TEAM_NAME_CH.get(a_abbr, a_abbr), 'a_id': a_id, 'a_abbr': a_abbr, 'a_l5': a_l5,
                            'prob': prob, 'diff': diff, 'winner': TEAM_NAME_CH.get(h_abbr if prob > 50 else a_abbr)
                        }

            if game_results:
                selected = st.selectbox("🎯 選擇場次", list(game_results.keys()), key=f"sel_{i}")
                res = game_results[selected]
                
                # 1. 預測卡片
                st.markdown(f"#### 🏟️ {selected}")
                c1, c2, c3 = st.columns(3)
                c1.metric(res['h_name'], f"{res['prob']:.1f}%", f"近5場: {res['h_l5']}")
                c2.metric(res['a_name'], f"{100 - res['prob']:.1f}%", f"近5場: {res['a_l5']}")
                c3.metric("模型預測贏家", res['winner'], f"分差 {res['diff']}")

                # 2. 數據對比區 (分為兩張表：效率 vs 體能)
                h_adv, a_adv = full_maps['adv'].get(res['h_id'], {}), full_maps['adv'].get(res['a_id'], {})
                h_ff, a_ff = full_maps['ff'].get(res['h_id'], {}), full_maps['ff'].get(res['a_id'], {})
                
                # 數據提取 helper
                def get_stat(d, k, fmt="{:.1f}"): return fmt.format(d.get(k, 0))

                st.markdown("---")
                c_tbl1, c_tbl2 = st.columns(2)
                
                with c_tbl1:
                    st.markdown("##### 📊 進階戰力 (Efficiency)")
                    comp_data = {
                        "指標": ["進攻效率", "防守效率", "真實命中 (TS%)", "有效命中 (eFG%)", "失誤率"],
                        res['h_name']: [h_adv.get('OFF_RATING'), h_adv.get('DEF_RATING'), get_stat(h_adv, 'TS_PCT', "{:.1%}"), get_stat(h_ff, 'EFG_PCT', "{:.1%}"), h_ff.get('TOV_PCT')],
                        res['a_name']: [a_adv.get('OFF_RATING'), a_adv.get('DEF_RATING'), get_stat(a_adv, 'TS_PCT', "{:.1%}"), get_stat(a_ff, 'EFG_PCT', "{:.1%}"), a_ff.get('TOV_PCT')]
                    }
                    st.table(pd.DataFrame(comp_data))

                with c_tbl2:
                    st.markdown("##### 🏃‍♂️ 體能與執行 (Tracking)")
                    h_spd, a_spd = full_maps['spd'].get(res['h_id'], {}), full_maps['spd'].get(res['a_id'], {})
                    h_pass, a_pass = full_maps['pass'].get(res['h_id'], {}), full_maps['pass'].get(res['a_id'], {})
                    h_hus, a_hus = full_maps['hustle'].get(res['h_id'], {}), full_maps['hustle'].get(res['a_id'], {})

                    track_data = {
                        "指標": ["跑動距離 (英里)", "平均速度 (MPH)", "場均傳球數", "撥球破壞 (Deflections)", "干擾投籃 (Contested)"],
                        res['h_name']: [h_spd.get('DIST_MILES'), h_spd.get('AVG_SPEED'), int(h_pass.get('PASSES_MADE', 0)), h_hus.get('DEFLECTIONS'), int(h_hus.get('CONTESTED_SHOTS', 0))],
                        res['a_name']: [a_spd.get('DIST_MILES'), a_spd.get('AVG_SPEED'), int(a_pass.get('PASSES_MADE', 0)), a_hus.get('DEFLECTIONS'), int(a_hus.get('CONTESTED_SHOTS', 0))]
                    }
                    st.table(pd.DataFrame(track_data))

                # 3. H2H
                st.markdown("##### ⚔️ 本季對戰歷史")
                h2h = gf[(gf['TEAM_ABBREVIATION'] == res['h_abbr']) & (gf['MATCHUP'].str.contains(res['a_abbr']))].sort_values('GAME_DATE', ascending=False)
                if not h2h.empty:
                    h2h['結果'] = h2h.apply(lambda r: f"W ({r.PTS}-{int(r.PTS-r.PLUS_MINUS)})" if r.WL == 'W' else f"L ({r.PTS}-{int(r.PTS-r.PLUS_MINUS)})", axis=1)
                    h2h_display = h2h[['GAME_DATE', 'MATCHUP', '結果']].rename(columns={'GAME_DATE': '日期', 'MATCHUP': '對戰', '結果': '賽果'})
                    h2h_display['日期'] = h2h_display['日期'].dt.strftime('%Y-%m-%d')
                    st.dataframe(h2h_display, hide_index=True, use_container_width=True)
                else: st.write("本季尚未交手。")

                # 4. 球員數據 (寬版)
                st.markdown("##### 🚀 預計出戰核心數據")
                def get_formatted_roster(tid):
                    df = ps_full[ps_full['TEAM_ID'] == tid].sort_values('PTS', ascending=False).head(8)
                    df = df[['PLAYER_NAME', 'PTS', 'TS_PCT', 'USG_PCT', 'E_NET_RATING', 'PIE']]
                    df.columns = ['球員', '得分', 'TS%', 'USG%', '淨效率', 'PIE']
                    return df

                st.subheader(f"🏠 {res['h_name']}")
                st.dataframe(get_formatted_roster(res['h_id']).style.format({'得分':'{:.1f}', 'TS%':'{:.1%}', 'USG%':'{:.1%}', '淨效率':'{:+.1f}', 'PIE':'{:.1%}'}), hide_index=True, use_container_width=True)
                
                st.subheader(f"✈️ {res['a_name']}")
                st.dataframe(get_formatted_roster(res['a_id']).style.format({'得分':'{:.1f}', 'TS%':'{:.1%}', 'USG%':'{:.1%}', '淨效率':'{:+.1f}', 'PIE':'{:.1%}'}), hide_index=True, use_container_width=True)

st.sidebar.caption(f"🕒 更新時間：{last_update}")
