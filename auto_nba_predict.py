import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2, leaguedashplayerstats
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
    'TOR': '多倫多暴龍', 'UTA': '猶他代理', 'WAS': '華盛頓巫師'
}

st.set_page_config(page_title="NBA 進階分析 v5.6", layout="wide")
st.title("🏀 NBA 終極數據預測 v5.6")

# --- 2. 核心數據載入 (集成進階指標到模型) ---
@st.cache_data(ttl=3600)
def load_all_data_v56():
    nba_teams = teams.get_teams()
    nba_ids = [t['id'] for t in nba_teams]
    
    # 1. 團隊進階數據 (TS%, NetRating) - 用於模型分析
    team_adv = leaguedashplayerstats.LeagueDashTeamStats(season='2025-26', measure_type_detailed_defense='Advanced').get_data_frames()[0]
    team_adv_map = team_adv.set_index('TEAM_ID')[['TS_PCT', 'E_NET_RATING', 'EFG_PCT']].to_dict('index')
    
    # 2. 歷史戰績
    gf_raw = leaguegamefinder.LeagueGameFinder(season_nullable='2025-26').get_data_frames()[0]
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    
    # 注入進階數據到每一行 (模型現在看得到 TS% 和 淨效率了)
    gf['TEAM_TS'] = gf['TEAM_ID'].map(lambda x: team_adv_map.get(x, {}).get('TS_PCT', 0.55))
    gf['TEAM_NET_RTG'] = gf['TEAM_ID'].map(lambda x: team_adv_map.get(x, {}).get('E_NET_RATING', 0))
    
    # 特徵工程
    gf['L10_WIN_RATE'] = gf.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    gf['L5_PLUS_MINUS'] = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    gf['B2B'] = (gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days == 1).astype(int)
    
    # 模型特徵組合 (包含進階數據)
    feats = ['L5_PLUS_MINUS', 'B2B', 'IS_HOME', 'L10_WIN_RATE', 'TEAM_TS', 'TEAM_NET_RTG']
    train = gf.fillna(0)
    clf = xgb.XGBClassifier().fit(train[feats], train['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(train[feats], train['PLUS_MINUS'])
    
    # 3. 球員進階數據 (用於下方表格顯示)
    ps_base = leaguedashplayerstats.LeagueDashPlayerStats(season='2025-26', per_mode_detailed='PerGame', measure_type_detailed_defense='Base').get_data_frames()[0]
    ps_adv = leaguedashplayerstats.LeagueDashPlayerStats(season='2025-26', per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced').get_data_frames()[0]
    ps_full = pd.merge(
        ps_base[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST']],
        ps_adv[['PLAYER_ID', 'TS_PCT', 'EFG_PCT', 'USG_PCT', 'E_NET_RATING']],
        on='PLAYER_ID', how='inner'
    )
    
    return clf, reg, gf, ps_full, feats, datetime.now(tw_tz).strftime("%H:%M:%S")

clf, reg, gf, ps_full, feats, last_update = load_all_data_v56()

# --- 3. 工具函數 ---
def get_fast_roster(team_id, ps_df):
    output = ps_df[ps_df['TEAM_ID'] == team_id].sort_values(by='PTS', ascending=False).head(8)
    if output.empty: return pd.DataFrame()
    res = output[['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT', 'EFG_PCT', 'USG_PCT', 'E_NET_RATING']]
    res.columns = ['球員', '得分', '籃板', '助攻', 'TS%', 'eFG%', 'USG%', '淨效率']
    return res

# --- 4. 介面渲染 ---
col_title, col_lock = st.columns([3, 1])
with col_lock:
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
                    h_data = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
                    if not h_data.empty:
                        prob = clf.predict_proba(h_data[feats])[0][1] * 100
                        diff = round(abs(float(reg.predict(h_data[feats])[0])))
                        
                        label = f"{TEAM_NAME_CH.get(a_abbr, a_abbr)} @ {TEAM_NAME_CH.get(h_abbr, h_abbr)}"
                        game_results[label] = {
                            'h_name': TEAM_NAME_CH.get(h_abbr, h_abbr), 'h_id': h_id,
                            'a_name': TEAM_NAME_CH.get(a_abbr, a_abbr), 'a_id': a_id,
                            'prob': prob, 'diff': diff,
                            'winner': TEAM_NAME_CH.get(h_abbr if prob > 50 else a_abbr)
                        }

            if game_results:
                selected = st.selectbox("🎯 選擇對戰", list(game_results.keys()), key=f"sel_{i}")
                res = game_results[selected]
                
                # 如果鎖定勝率，這裡會顯示固定的視覺效果
                st.markdown(f"#### 🏟️ {selected} (分析包含 TS% / NetRating)")
                c1, c2, c3 = st.columns(3)
                c1.metric(res['h_name'], f"{res['prob']:.1f}%")
                c2.metric(res['a_name'], f"{100 - res['prob']:.1f}%")
                c3.metric("預估贏家", res['winner'], f"分差 {res['diff']}")
                
                # 表格顯示
                st.markdown("---")
                st.markdown("##### 🚀 核心球員進階數據分析")
                def format_style(df):
                    return df.style.format({
                        '得分': '{:.1f}', '籃板': '{:.1f}', '助攻': '{:.1f}',
                        'TS%': '{:.1%}', 'eFG%': '{:.1%}', 'USG%': '{:.1%}', '淨效率': '{:+.1f}'
                    })
                col_l, col_r = st.columns(2)
                with col_l:
                    st.write(f"🏠 {res['h_name']}")
                    st.dataframe(format_style(get_fast_roster(res['h_id'], ps_full)), hide_index=True, use_container_width=True)
                with col_r:
                    st.write(f"✈️ {res['a_name']}")
                    st.dataframe(format_style(get_fast_roster(res['a_id'], ps_full)), hide_index=True, use_container_width=True)

st.sidebar.caption(f"🕒 數據最後更新：{last_update}")
