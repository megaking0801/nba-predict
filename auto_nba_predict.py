import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    synergyplaytypes, leaguedashptdefend
)
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import pytz, warnings, requests, unicodedata
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# --- 1. 基本設定 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')
us_east_tz = pytz.timezone('US/Eastern')

# 擴充 ESPN 對應字典
TEAM_NAME_EN_MAP = {
    'Atlanta': 'ATL', 'Brooklyn': 'BKN', 'Boston': 'BOS', 'Charlotte': 'CHA',
    'Chicago': 'CHI', 'Cleveland': 'CLE', 'Dallas': 'DAL', 'Denver': 'DEN',
    'Detroit': 'DET', 'Golden State': 'GSW', 'Houston': 'HOU', 'Indiana': 'IND',
    'LA Clippers': 'LAC', 'LA Lakers': 'LAL', 'Memphis': 'MEM', 'Miami': 'MIA',
    'Milwaukee': 'MIL', 'Minnesota': 'MIN', 'New Orleans': 'NOP', 'New York': 'NYK',
    'Oklahoma City': 'OKC', 'Orlando': 'ORL', 'Philadelphia': 'PHI', 'Phoenix': 'PHX',
    'Portland': 'POR', 'Sacramento': 'SAC', 'San Antonio': 'SAS', 'Toronto': 'TOR',
    'Utah': 'UTA', 'Washington': 'WAS'
}

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

st.set_page_config(page_title="NBA 數據專家 v7.5", layout="wide")
st.title("🏀 NBA 數據專家 v7.5 (ESPN 傷病源+修復版)")

# --- 2. 工具函數 ---
def normalize_name(name):
    if not isinstance(name, str): return ""
    name = unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode("utf-8")
    return name.lower().replace('.', '').strip()

def fetch_safe_df(endpoint_class, **kwargs):
    try:
        instance = endpoint_class(**kwargs)
        raw = instance.get_dict()
        res = raw['resultSets'][0] if 'resultSets' in raw else raw['resultSet']
        df = pd.DataFrame(res['rowSet'], columns=res['headers'])
        if 'TEAM_ID' in df.columns: df['TEAM_ID'] = df['TEAM_ID'].astype(int)
        return df
    except: return pd.DataFrame()

# --- 3. 強化版傷病爬蟲 (來源：ESPN) ---
@st.cache_data(ttl=600)
def fetch_live_injuries_espn():
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        injury_data = {}
        
        # ESPN 結構：每個隊伍是一個 Table__Title
        sections = soup.find_all(class_='Table__Title')
        for section in sections:
            team_name_raw = section.get_text().strip()
            abbr = next((a for n, a in TEAM_NAME_EN_MAP.items() if n in team_name_raw), None)
            if not abbr: continue
            
            table = section.find_next('table')
            rows = table.find_all('tr')[1:] # 跳過表頭
            team_injuries = []
            for row in rows:
                cols = row.find_all('td')
                if len(cols) >= 3:
                    p_name = cols[0].get_text(strip=True)
                    p_status = cols[2].get_text(strip=True)
                    
                    # 包含報銷/缺陣關鍵字
                    out_kws = ['out', 'season', 'surgery', 'indefinitely', 'broken', 'torn']
                    is_out = any(k in p_status.lower() for k in out_kws)
                    is_dqs = any(k in p_status.lower() for k in ['day-to-day', 'questionable', 'doubtful'])
                    
                    team_injuries.append({
                        'name': p_name,
                        'status': p_status,
                        'is_out': is_out,
                        'is_dqs': is_dqs
                    })
            injury_data[abbr] = team_injuries
        return injury_data
    except Exception as e:
        st.sidebar.warning(f"ESPN 抓取異常: {e}")
        return {}

# --- 4. 數據加載與模型訓練 (修復 feats 返回問題) ---
@st.cache_data(ttl=3600)
def load_all_data_v75():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S, ST = '2025-26', 'Regular Season'
    
    # 抓取各式 Maps (v6.9 全功能保留)
    df_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_hustle = fetch_safe_df(leaguehustlestatsteam.LeagueHustleStatsTeam, season=S, per_mode_time='PerGame')
    df_track_spd = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
    
    def to_map(df, cols): return df.set_index('TEAM_ID')[cols].to_dict('index') if not df.empty else {}
    maps = {
        'base': to_map(df_base, ['PTS', 'REB', 'AST', 'FG_PCT']),
        'adv': to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE']),
        'hustle': to_map(df_hustle, ['DEFLECTIONS', 'CONTESTED_SHOTS']),
        'spd': to_map(df_track_spd, ['DIST_MILES', 'AVG_SPEED']),
        'trans': to_map(df_trans, ['PPP'])
    }

    # 模型訓練 (包含勝率與分差)
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    
    feats = ['REST_DAYS']
    train_df = gf.fillna(0)
    clf = xgb.XGBClassifier().fit(train_df[feats], train_df['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(train_df[feats], train_df['PLUS_MINUS'])
    
    # 球員數據庫 (用於比對 PPG)
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST']], ps_adv[['PLAYER_ID', 'TS_PCT']], on='PLAYER_ID')
    
    player_stats_db = {normalize_name(row['PLAYER_NAME']): row['PTS'] for _, row in ps_full.iterrows()}
    
    return clf, reg, gf, ps_full, feats, maps, player_stats_db, datetime.now(tw_tz).strftime("%H:%M")

# 執行加載
clf, reg, gf, ps_full, feats, maps, player_stats_db, last_update = load_all_data_v75()
injury_report = fetch_live_injuries_espn()

# --- 5. 傷病計算邏輯 (修正：強制顯示 0 分球員) ---
def get_injury_impact_v75(injuries, db):
    score, details = 0, []
    for inj in injuries:
        ppg = db.get(normalize_name(inj['name']), 0)
        weight = 1.0 if inj['is_out'] else (0.5 if inj['is_dqs'] else 0.3)
        
        # 扣分權重
        penalty = 0
        if ppg >= 25: penalty = 12
        elif ppg >= 18: penalty = 7
        elif ppg >= 10: penalty = 3
        elif ppg >= 5: penalty = 1
        
        final_p = penalty * weight
        score += final_p
        
        # 標註圖示
        status_icon = "❌" if inj['is_out'] else "⚠️"
        impact_label = f"-{final_p:.1f}%" if final_p > 0 else "(影響輕微/角色球員)"
        details.append(f"{status_icon} {inj['name']} [{inj['status']}] (場均 {ppg:.1f}分) {impact_label}")
        
    return score, details

# --- 6. 介面呈現 ---
nba_now = datetime.now(us_east_tz)
dates_nba = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates_nba])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates_nba[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 目前無比賽資訊")
            continue

        id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        games_list = []
        for _, row in sb.iterrows():
            h_abbr, a_abbr = id_to_abbr.get(row['HOME_TEAM_ID']), id_to_abbr.get(row['VISITOR_TEAM_ID'])
            if h_abbr and a_abbr:
                games_list.append(f"{TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}")

        if games_list:
            selected = st.selectbox("🎯 選擇場次", games_list, key=f"s_{i}")
            a_ch, h_ch = selected.split(" @ ")
            h_abbr = [k for k, v in TEAM_NAME_CH.items() if v == h_ch][0]
            a_abbr = [k for k, v in TEAM_NAME_CH.items() if v == a_ch][0]
            h_id = [t['id'] for t in teams.get_teams() if t['abbreviation'] == h_abbr][0]
            a_id = [t['id'] for t in teams.get_teams() if t['abbreviation'] == a_abbr][0]

            # 計算預測
            h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
            # --- 修正 NameError：確保 feats 是全域可用的 ---
            base_prob = clf.predict_proba(h_last[feats])[0][1] * 100 if not h_last.empty else 50.0
            proj_diff = reg.predict(h_last[feats])[0] if not h_last.empty else 0.0
            
            h_impact, h_details = get_injury_impact_v75(injury_report.get(h_abbr, []), player_stats_db)
            a_impact, a_details = get_injury_impact_v75(injury_report.get(a_abbr, []), player_stats_db)
            
            # 傷病直接修正勝率
            final_prob = max(5, min(95, base_prob - h_impact + a_impact))

            st.markdown(f"### 🏟️ {selected}")
            col1, col2, col3 = st.columns(3)
            col1.metric(h_ch, f"{final_prob:.1f}%", f"傷病修正 -{h_impact:.1f}%")
            col2.metric(a_ch, f"{100-final_prob:.1f}%", f"傷病修正 -{a_impact:.1f}%")
            col3.metric("AI 預估分差", f"{proj_diff:+.1f}")

            # --- 🚑 強制顯示所有傷病 ---
            st.subheader("🚑 即時傷病名單 (含報銷球員)")
            ic1, ic2 = st.columns(2)
            with ic1:
                st.write(f"**{h_ch}**")
                if h_details:
                    for d in h_details: st.markdown(d)
                else: st.success("目前健康")
            with ic2:
                st.write(f"**{a_ch}**")
                if a_details:
                    for d in a_details: st.markdown(d)
                else: st.success("目前健康")

            # --- 📊 團隊深度數據 (v6.9 經典表格) ---
            st.divider()
            def get_m(m, tid, k): return maps[m].get(int(tid), {}).get(k, 0)
            st.subheader("📊 關鍵數據對比")
            st.table(pd.DataFrame({
                "項目": ["進攻效率", "防守效率", "節奏 (Pace)", "干擾投籃", "轉換得分(PPP)"],
                h_ch: [get_m('adv', h_id, 'OFF_RATING'), get_m('adv', h_id, 'DEF_RATING'), get_m('adv', h_id, 'PACE'), get_m('hustle', h_id, 'CONTESTED_SHOTS'), get_m('trans', h_id, 'PPP')],
                a_ch: [get_m('adv', a_id, 'OFF_RATING'), get_m('adv', a_id, 'DEF_RATING'), get_m('adv', a_id, 'PACE'), get_m('hustle', a_id, 'CONTESTED_SHOTS'), get_m('trans', a_id, 'PPP')]
            }))

            st.subheader("🚀 核心球員 Top 6")
            for tid, name in [(h_id, h_ch), (a_id, a_ch)]:
                st.write(f"**{name}**")
                p_df = ps_full[ps_full['TEAM_ID'] == tid].sort_values('PTS', ascending=False).head(6)
                st.dataframe(p_df[['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分','TS_PCT':'真實命中%'}), hide_index=True)

st.sidebar.caption(f"🕒 更新時間：{last_update}")
st.sidebar.write(f"🚑 ESPN 總共抓到 {sum(len(v) for v in injury_report.values())} 位傷兵")
