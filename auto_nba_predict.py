import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2, leaguedashplayerstats, commonteamroster
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

st.set_page_config(page_title="NBA 進階數據專家 v5.4", layout="wide")
st.title("🏀 NBA 終極進階數據分析 v5.4")

# --- 2. 核心數據載入 (包含進階數據) ---
@st.cache_data(ttl=3600)
def load_advanced_data():
    nba_ids = [t['id'] for t in teams.get_teams()]
    # 基礎戰績
    gf_raw = leaguegamefinder.LeagueGameFinder(season_nullable='2025-26').get_data_frames()[0]
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    
    # 計算團隊特徵
    gf['L10_WIN_RATE'] = gf.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    gf['L5_PTS'] = gf.groupby('TEAM_ID')['PTS'].transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    gf['L5_PLUS_MINUS'] = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    gf['B2B'] = (gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days == 1).astype(int)
    
    feats = ['L5_PTS', 'L5_PLUS_MINUS', 'B2B', 'IS_HOME', 'L10_WIN_RATE']
    train = gf.fillna(0)
    clf = xgb.XGBClassifier().fit(train[feats], train['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(train[feats], train['PLUS_MINUS'])
    
    # 抓取球員「基礎」場均數據
    ps_base = leaguedashplayerstats.LeagueDashPlayerStats(season='2025-26', per_mode_detailed='PerGame', measure_type_detailed_defense='Base').get_data_frames()[0]
    ps_base = ps_base[['PLAYER_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST', 'FG_PCT', 'FG3_PCT', 'FT_PCT']]
    
    # 抓取球員「進階」數據 (TS%, eFG%, USG%, Net Rating)
    ps_adv = leaguedashplayerstats.LeagueDashPlayerStats(season='2025-26', per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced').get_data_frames()[0]
    ps_adv = ps_adv[['PLAYER_ID', 'E_OFF_RATING', 'E_DEF_RATING', 'E_NET_RATING', 'EFG_PCT', 'TS_PCT', 'USG_PCT']]
    
    # 合併球員數據
    ps_full = pd.merge(ps_base, ps_adv, on='PLAYER_ID', how='inner')
    
    return clf, reg, gf, ps_full, feats

clf, reg, gf, ps_full, feats = load_advanced_data()

# --- 3. 球員名單與進階指標 ---
def get_advanced_roster(team_id, ps_df):
    try:
        ros = commonteamroster.CommonTeamRoster(team_id=team_id).get_data_frames()[0]
        name_col = 'PLAYER' if 'PLAYER' in ros.columns else 'PLAYER_NAME'
        ros = ros.rename(columns={name_col: 'PLAYER_NAME'})
        
        merged = pd.merge(ros, ps_df, on='PLAYER_NAME', how='left').fillna(0)
        # 篩選核心 8 人 (依得分排序)
        final = merged.sort_values(by='PTS', ascending=False).head(8)
        
        # 整理輸出表格
        output = final[[
            'PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT', 'EFG_PCT', 'USG_PCT', 'E_NET_RATING'
        ]]
        output.columns = ['球員', '得分', '籃板', '助攻', '真實命中率(TS%)', '有效命中率(eFG%)', '使用率(USG%)', '淨效率值']
        return output
    except:
        return pd.DataFrame()

# --- 4. 介面渲染 ---
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        current_date = dates[i]
        date_api = current_date.strftime('%m/%d/%Y')
        
        try:
            sb = scoreboardv2.ScoreboardV2(game_date=date_api).get_data_frames()[0]
        except:
            sb = pd.DataFrame()

        if sb.empty:
            st.info(f"📅 {current_date.strftime('%Y-%m-%d')} 目前無賽程。")
        else:
            id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
            game_options = []
            game_results = {}

            for _, row in sb.iterrows():
                h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
                h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
                
                if h_abbr and a_abbr:
                    h_data = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
                    a_data = gf[gf['TEAM_ABBREVIATION'] == a_abbr].tail(1)
                    
                    if not h_data.empty and not a_data.empty:
                        prob = clf.predict_proba(h_data[feats])[0][1] * 100
                        diff = round(abs(float(reg.predict(h_data[feats])[0]) - float(reg.predict(a_data[feats])[0])))
                        
                        label = f"{TEAM_NAME_CH.get(a_abbr, a_abbr)} @ {TEAM_NAME_CH.get(h_abbr, h_abbr)}"
                        game_options.append(label)
                        game_results[label] = {
                            'h_name': TEAM_NAME_CH.get(h_abbr, h_abbr), 'h_id': h_id,
                            'a_name': TEAM_NAME_CH.get(a_abbr, a_abbr), 'a_id': a_id,
                            'h_prob': prob, 'a_prob': 100 - prob,
                            'diff': diff, 'winner': TEAM_NAME_CH.get(h_abbr if prob > 50 else a_abbr)
                        }

            if game_options:
                selected = st.selectbox("🎯 選擇場次", game_options, key=f"sel_{i}")
                res = game_results[selected]
                
                # 戰勝與分差指標
                st.markdown(f"#### 🏟️ {selected} 深度數據預測")
                c1, c2, c3 = st.columns(3)
                c1.metric(res['h_name'], f"{res['h_prob']:.1f}%")
                c2.metric(res['a_name'], f"{res['a_prob']:.1f}%")
                c3.metric("預測贏家", res['winner'], f"分差 {res['diff']}")
                
                # 進階球員數據表格
                st.markdown("---")
                st.markdown("##### 🚀 預計出戰核心球員進階數據 (場均)")
                
                # 格式化函數
                def format_adv_table(df):
                    return df.style.format({
                        '得分': '{:.1f}', '籃板': '{:.1f}', '助攻': '{:.1f}',
                        '真實命中率(TS%)': '{:.1%}', '有效命中率(eFG%)': '{:.1%}',
                        '使用率(USG%)': '{:.1%}', '淨效率值': '{:+.1f}'
                    })

                st.write(f"🏠 {res['h_name']}")
                st.dataframe(format_adv_table(get_advanced_roster(res['h_id'], ps_full)), hide_index=True, use_container_width=True)
                
                st.write(f"✈️ {res['a_name']}")
                st.dataframe(format_adv_table(get_advanced_roster(res['a_id'], ps_full)), hide_index=True, use_container_width=True)
            else:
                st.warning("查無分析數據。")
