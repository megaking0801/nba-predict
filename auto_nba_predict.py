import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2, leaguedashplayerstats, commonteamroster
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import pytz, warnings, json
from datetime import datetime, timedelta
from google import genai

# --- 1. AI & 基本設定 (持續記住每次更動) ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')

@st.cache_resource
def get_ai_client():
    if "GEMINI_API_KEY" in st.secrets:
        try:
            return genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
        except: return None
    return None

client = get_ai_client()

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

st.set_page_config(page_title="NBA AI v5.2", layout="wide")
st.title("🏀 NBA 終極預測專家 v5.2")

# --- 2. 核心數據載入 (場均數據抓取) ---
@st.cache_data(ttl=3600)
def load_base_data():
    nba_ids = [t['id'] for t in teams.get_teams()]
    gf_raw = leaguegamefinder.LeagueGameFinder(season_nullable='2025-26').get_data_frames()[0]
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['L10_WIN_RATE'] = gf.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    gf['L5_PTS'] = gf.groupby('TEAM_ID')['PTS'].transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    gf['L5_PLUS_MINUS'] = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    gf['B2B'] = (gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days == 1).astype(int)
    
    feats = ['L5_PTS', 'L5_PLUS_MINUS', 'B2B', 'IS_HOME', 'L10_WIN_RATE']
    train = gf.fillna(0)
    
    clf = xgb.XGBClassifier().fit(train[feats], train['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(train[feats], train['PLUS_MINUS'])
    
    # 關鍵：抓取 PerGame 場均數據
    ps = leaguedashplayerstats.LeagueDashPlayerStats(season='2025-26', per_mode_detailed='PerGame').get_data_frames()[0]
    # 只取場均得分、籃板、助攻
    ps = ps[['PLAYER_NAME', 'PTS', 'REB', 'AST']]
    
    return clf, reg, gf, ps, feats

clf, reg, gf, ps, feats = load_base_data()

# --- 3. 球員場均數據處理 ---
def get_team_roster_stats(team_id, player_stats_df):
    try:
        ros = commonteamroster.CommonTeamRoster(team_id=team_id).get_data_frames()[0]
        name_col = 'PLAYER' if 'PLAYER' in ros.columns else 'PLAYER_NAME'
        ros = ros.rename(columns={name_col: 'PLAYER_NAME'})
        
        merged = ros.merge(player_stats_df, on='PLAYER_NAME', how='left').fillna(0)
        # 排序並取前 5 名得分手
        final = merged[['PLAYER_NAME', 'PTS', 'REB', 'AST']].sort_values(by='PTS', ascending=False).head(5)
        # 重新命名以符合「場均」描述
        final.columns = ['球員姓名', '場均得分', '場均籃板', '場均助攻']
        return final
    except:
        return pd.DataFrame(columns=['球員姓名', '場均得分', '場均籃板', '場均助攻'])

# --- 4. 日期分頁與顯示 ---
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
                
                # 數據指標卡
                st.markdown(f"#### 🏟️ {selected}")
                c1, c2, c3 = st.columns(3)
                c1.metric(res['h_name'], f"{res['h_prob']:.1f}%")
                c2.metric(res['a_name'], f"{res['a_prob']:.1f}%")
                c3.metric("預測勝方", res['winner'], f"分差 {res['diff']}")
                
                if client:
                    if st.button("🪄 生成 AI 專家報告", key=f"ai_{i}_{selected}"):
                        with st.spinner("AI 分析中..."):
                            prompt = f"你是 NBA 專家。分析比賽：{selected}，預測贏家 {res['winner']}，分差約 {res['diff']} 分。請寫 180 字分析。"
                            response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
                            st.info(response.text)
                
                # 核心球員場均表格
                st.markdown("##### 📊 核心球員場均數據 (Top 5)")
                l_col, r_col = st.columns(2)
                with l_col:
                    st.write(f"🏠 {res['h_name']}")
                    st.dataframe(get_team_roster_stats(res['h_id'], ps).style.format({
                        '場均得分': '{:.1f}', '場均籃板': '{:.1f}', '場均助攻': '{:.1f}'
                    }), hide_index=True)
                with r_col:
                    st.write(f"✈️ {res['a_name']}")
                    st.dataframe(get_team_roster_stats(res['a_id'], ps).style.format({
                        '場均得分': '{:.1f}', '場均籃板': '{:.1f}', '場均助攻': '{:.1f}'
                    }), hide_index=True)
            else:
                st.warning("查無匹配的歷史數據，無法預測。")
