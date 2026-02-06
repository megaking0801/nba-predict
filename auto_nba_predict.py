import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2, leaguedashplayerstats, leaguedashteamstats
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

st.set_page_config(page_title="NBA 終極分析 v5.9", layout="wide")
st.title("🏀 NBA 數據預測專家 v5.9 (全數據集成)")

# --- 2. 核心數據載入 (包含四大因素與效率指標) ---
@st.cache_data(ttl=3600)
def load_full_analytical_data():
    nba_ids = [t['id'] for t in teams.get_teams()]
    
    # A. 抓取團隊「進階效率」與「四大因素」
    team_adv = leaguedashteamstats.LeagueDashTeamStats(season='2025-26', measure_type_detailed_defense='Advanced').get_data_frames()[0]
    team_ff = leaguedashteamstats.LeagueDashTeamStats(season='2025-26', measure_type_detailed_defense='FourFactors').get_data_frames()[0]
    
    # 建立數據映射
    adv_map = team_adv.set_index('TEAM_ID')[['OFF_RATING', 'DEF_RATING', 'NET_RATING', 'PACE', 'TS_PCT']].to_dict('index')
    ff_map = team_ff.set_index('TEAM_ID')[['EFG_PCT', 'TOV_PCT', 'OREB_PCT', 'FTA_RATE']].to_dict('index')

    # B. 歷史戰績與休息天數
    gf_raw = leaguegamefinder.LeagueGameFinder(season_nullable='2025-26').get_data_frames()[0]
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    
    # 計算休息天數 (Rest Days)
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    gf['REST_DAYS'] = gf['REST_DAYS'].apply(lambda x: 3 if x > 3 else x) # 超過三天視為充足休息
    
    # 注入模型特徵
    gf['TEAM_ORTG'] = gf['TEAM_ID'].map(lambda x: adv_map.get(x, {}).get('OFF_RATING', 110))
    gf['TEAM_DRTG'] = gf['TEAM_ID'].map(lambda x: adv_map.get(x, {}).get('DEF_RATING', 110))
    gf['TEAM_EFG'] = gf['TEAM_ID'].map(lambda x: ff_map.get(x, {}).get('EFG_PCT', 0.5))
    gf['TEAM_TOV'] = gf['TEAM_ID'].map(lambda x: ff_map.get(x, {}).get('TOV_PCT', 15))
    
    # 建立模型
    feats = ['IS_HOME', 'REST_DAYS', 'TEAM_ORTG', 'TEAM_DRTG', 'TEAM_EFG', 'TEAM_TOV']
    train = gf.fillna(0)
    clf = xgb.XGBClassifier().fit(train[feats], train['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(train[feats], train['PLUS_MINUS'])
    
    # C. 球員進階數據 (表格顯示)
    ps_base = leaguedashplayerstats.LeagueDashPlayerStats(season='2025-26', per_mode_detailed='PerGame').get_data_frames()[0]
    ps_adv = leaguedashplayerstats.LeagueDashPlayerStats(season='2025-26', per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced').get_data_frames()[0]
    ps_full = pd.merge(
        ps_base[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST']],
        ps_adv[['PLAYER_ID', 'TS_PCT', 'EFG_PCT', 'USG_PCT', 'E_NET_RATING', 'PIE']], # PIE 是綜合表現指標
        on='PLAYER_ID', how='inner'
    )
    
    return clf, reg, gf, ps_full, feats, adv_map, ff_map

clf, reg, gf, ps_full, feats, adv_map, ff_map = load_full_analytical_data()

# --- 3. 介面渲染與對戰分析 ---
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
            st.info(f"📅 {current_date.strftime('%Y-%m-%d')} 目前無賽程。")
        else:
            id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
            game_results = {}

            for _, row in sb.iterrows():
                h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
                h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
                if h_abbr and a_abbr:
                    # 獲取最新狀態
                    h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
                    if not h_last.empty:
                        prob = clf.predict_proba(h_last[feats])[0][1] * 100
                        diff = round(abs(float(reg.predict(h_last[feats])[0])))
                        
                        label = f"{TEAM_NAME_CH.get(a_abbr, a_abbr)} @ {TEAM_NAME_CH.get(h_abbr, h_abbr)}"
                        game_results[label] = {
                            'h_name': TEAM_NAME_CH.get(h_abbr, h_abbr), 'h_id': h_id, 'h_abbr': h_abbr,
                            'a_name': TEAM_NAME_CH.get(a_abbr, a_abbr), 'a_id': a_id, 'a_abbr': a_abbr,
                            'prob': prob, 'diff': diff,
                            'winner': TEAM_NAME_CH.get(h_abbr if prob > 50 else a_abbr)
                        }

            if game_results:
                selected = st.selectbox("🎯 選擇對戰場次", list(game_results.keys()), key=f"sel_{i}")
                res = game_results[selected]
                
                # 1. 核心指標卡
                st.markdown(f"#### 🏟️ {selected} (已集成 4 Factors 分析)")
                c1, c2, c3 = st.columns(3)
                c1.metric(res['h_name'], f"{res['prob']:.1f}%")
                c2.metric(res['a_name'], f"{100 - res['prob']:.1f}%")
                c3.metric("預測勝方", res['winner'], f"分差 {res['diff']}")

                # 2. 團隊四大因素對比表
                st.markdown("---")
                st.markdown("##### 📊 團隊戰力關鍵指標對比 (Efficiency & Four Factors)")
                h_adv, a_adv = adv_map.get(res['h_id'], {}), adv_map.get(res['a_id'], {})
                h_ff, a_ff = ff_map.get(res['h_id'], {}), ff_map.get(res['a_id'], {})
                
                comp_data = {
                    "指標": ["進攻效率 (ORTG)", "防守效率 (DRTG)", "淨效率 (NET)", "真實命中率 (TS%)", "有效命中率 (eFG%)", "失誤率 (TOV%)", "比賽節奏 (PACE)"],
                    res['h_name']: [h_adv.get('OFF_RATING'), h_adv.get('DEF_RATING'), h_adv.get('NET_RATING'), f"{h_adv.get('TS_PCT'):.1%}", f"{h_ff.get('EFG_PCT'):.1%}", f"{h_ff.get('TOV_PCT'):.1f}", h_adv.get('PACE')],
                    res['a_name']: [a_adv.get('OFF_RATING'), a_adv.get('DEF_RATING'), a_adv.get('NET_RATING'), f"{a_adv.get('TS_PCT'):.1%}", f"{a_ff.get('EFG_PCT'):.1%}", f"{a_ff.get('TOV_PCT'):.1f}", a_adv.get('PACE')]
                }
                st.table(pd.DataFrame(comp_data))

                # 3. 本季交手紀錄 (H2H)
                st.markdown("##### ⚔️ 本賽季歷史對戰紀錄")
                h2h = gf[(gf['TEAM_ABBREVIATION'] == res['h_abbr']) & (gf['MATCHUP'].str.contains(res['a_abbr']))].sort_values('GAME_DATE', ascending=False)
                if not h2h.empty:
                    h2h['結果'] = h2h.apply(lambda r: f"W ({r.PTS}-{int(r.PTS-r.PLUS_MINUS)})" if r.WL == 'W' else f"L ({r.PTS}-{int(r.PTS-r.PLUS_MINUS)})", axis=1)
                    st.dataframe(h2h[['GAME_DATE', 'MATCHUP', '結果']].assign(GAME_DATE=h2h['GAME_DATE'].dt.strftime('%Y-%m-%d')), hide_index=True)
                else: st.write("本季尚未交手。")

                # 4. 球員進階數據 (Top 8)
                st.markdown("##### 🚀 核心球員進階數據 (PIE 為綜合貢獻值)")
                def get_roster(tid):
                    df = ps_full[ps_full['TEAM_ID'] == tid].sort_values('PTS', ascending=False).head(8)
                    return df[['PLAYER_NAME', 'PTS', 'TS_PCT', 'USG_PCT', 'E_NET_RATING', 'PIE']]

                cl, cr = st.columns(2)
                with cl:
                    st.write(f"🏠 {res['h_name']}")
                    st.dataframe(get_roster(res['h_id']).style.format({'PTS': '{:.1f}', 'TS_PCT': '{:.1%}', 'USG_PCT': '{:.1%}', 'E_NET_RATING': '{:+.1f}', 'PIE': '{:.1%}'}), hide_index=True)
                with cr:
                    st.write(f"✈️ {res['a_name']}")
                    st.dataframe(get_roster(res['a_id']).style.format({'PTS': '{:.1f}', 'TS_PCT': '{:.1%}', 'USG_PCT': '{:.1%}', 'E_NET_RATING': '{:+.1f}', 'PIE': '{:.1%}'}), hide_index=True)
