import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    synergyplaytypes
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

st.set_page_config(page_title="NBA 數據專家 v8.1", layout="wide")

# 初始化 Session State 用於儲存賠率
if 'saved_odds' not in st.session_state:
    st.session_state.saved_odds = {}
if 'odds_locked' not in st.session_state:
    st.session_state.odds_locked = False

# --- 2. 工具與數據載入 (與前版本一致) ---
def normalize_name(name):
    if not isinstance(name, str): return ""
    return unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode("utf-8").lower().replace('.', '').strip()

def fetch_safe_df(endpoint_class, **kwargs):
    try:
        instance = endpoint_class(**kwargs)
        raw = instance.get_dict()
        res = raw['resultSets'][0] if 'resultSets' in raw else raw['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

@st.cache_data(ttl=600)
def fetch_live_injuries_espn():
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        injury_data = {}
        for section in soup.find_all(class_='Table__Title'):
            team_raw = section.get_text().strip()
            abbr = next((a for n, a in TEAM_NAME_EN_MAP.items() if n in team_raw), None)
            if not abbr: continue
            rows = section.find_next('table').find_all('tr')[1:]
            team_inj = []
            for row in rows:
                cols = row.find_all('td')
                if len(cols) >= 3:
                    name = cols[0].get_text(strip=True)
                    status = cols[2].get_text(strip=True)
                    is_out = any(k in status.lower() for k in ['out', 'season', 'surgery', 'indefinitely', 'broken', 'torn'])
                    team_inj.append({'name': name, 'status': status, 'is_out': is_out})
            injury_data[abbr] = team_inj
        return injury_data
    except: return {}

@st.cache_data(ttl=3600)
def load_all_data_v81():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S, ST = '2025-26', 'Regular Season'
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST']], ps_adv[['PLAYER_ID', 'TS_PCT']], on='PLAYER_ID')
    player_stats_db = {normalize_name(row['PLAYER_NAME']): row['PTS'] for _, row in ps_full.iterrows()}
    
    # 團隊數據 Maps
    df_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_hustle = fetch_safe_df(leaguehustlestatsteam.LeagueHustleStatsTeam, season=S, per_mode_time='PerGame')
    df_spd = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
    df_pass = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='Passing', per_mode_simple='PerGame')
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
    
    def to_map(df, cols): return df.set_index('TEAM_ID')[cols].to_dict('index') if not df.empty else {}
    maps = {'base': to_map(df_base, ['PTS', 'REB', 'AST', 'FG_PCT']), 'adv': to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE']), 'hustle': to_map(df_hustle, ['DEFLECTIONS', 'CONTESTED_SHOTS']), 'spd': to_map(df_spd, ['DIST_MILES', 'AVG_SPEED']), 'pass': to_map(df_pass, ['PASSES_MADE']), 'trans': to_map(df_trans, ['PPP'])}
    
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    clf = xgb.XGBClassifier().fit(gf[['REST_DAYS']].fillna(0), gf['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(gf[['REST_DAYS']].fillna(0), gf['PLUS_MINUS'].fillna(0))
    
    return clf, reg, gf, ps_full, maps, player_stats_db, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, ps_full, maps, player_stats_db, last_update = load_all_data_v81()
injury_report = fetch_live_injuries_espn()

# --- 3. 核心邏輯 ---
def get_injury_impact(abbr, db):
    injuries = injury_report.get(abbr, [])
    score, details = 0, []
    for inj in injuries:
        ppg = db.get(normalize_name(inj['name']), 0)
        penalty = 12 if ppg >= 25 else (7 if ppg >= 18 else (3 if ppg >= 10 else (1 if ppg >= 5 else 0)))
        score += penalty
        icon = "❌" if inj['is_out'] else "⚠️"
        details.append(f"{icon} {inj['name']} (場均{ppg:.1f}) -{penalty:.1f}%")
    return score, details

# --- 4. 介面呈現 ---
st.title("🏀 NBA 數據專家 v8.1 (賠率鎖定版)")

nba_now = datetime.now(us_east_tz)
dates_nba = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates_nba])

for i, tab in enumerate(tabs):
    with tab:
        current_date_str = dates_nba[i].strftime('%Y-%m-%d')
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates_nba[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 目前無比賽資訊")
            continue

        id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        analysis_results = []

        # --- 賠率鎖定控制區 ---
        st.subheader("💰 賠率輸入與鎖定")
        is_locked = st.toggle("🔒 鎖定賠率 (防止重新載入時跳掉)", key=f"lock_{i}")
        
        with st.expander("展開輸入當前賠率", expanded=not is_locked):
            o_cols = st.columns(3)
            for idx, row in sb.iterrows():
                h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
                h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
                if not h_abbr or not a_abbr: continue
                
                game_key = f"{current_date_str}_{a_abbr}_{h_abbr}"
                # 從記憶體讀取或預設
                default_h = st.session_state.saved_odds.get(f"{game_key}_h", 1.75)
                default_a = st.session_state.saved_odds.get(f"{game_key}_a", 1.75)

                with o_cols[len(analysis_results) % 3]:
                    st.write(f"**{TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}**")
                    oh = st.number_input(f"🏠 {h_abbr}", value=default_h, step=0.01, key=f"input_h_{game_key}", disabled=is_locked)
                    oa = st.number_input(f"✈️ {a_abbr}", value=default_a, step=0.01, key=f"input_a_{game_key}", disabled=is_locked)
                    
                    # 即時更新記憶體
                    st.session_state.saved_odds[f"{game_key}_h"] = oh
                    st.session_state.saved_odds[f"{game_key}_a"] = oa
                
                # 計算 AI 指標
                h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
                base_p = clf.predict_proba(h_last[['REST_DAYS']])[0][1]*100 if not h_last.empty else 50.0
                base_m = reg.predict(h_last[['REST_DAYS']])[0] if not h_last.empty else 0.0
                h_imp, h_det = get_injury_impact(h_abbr, player_stats_db)
                a_imp, a_det = get_injury_impact(a_abbr, player_stats_db)
                final_p_h = max(5, min(95, base_p - h_imp + a_imp))
                
                imp_h = (1/oh)/(1/oh + 1/oa) * 100
                imp_a = (1/oa)/(1/oh + 1/oa) * 100
                
                analysis_results.append({
                    'label': f"{TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}",
                    'h_ch': TEAM_NAME_CH.get(h_abbr), 'a_ch': TEAM_NAME_CH.get(a_abbr),
                    'h_id': h_id, 'a_id': a_id, 'final_p_h': final_p_h, 'final_m_h': base_m - (h_imp/3) + (a_imp/3),
                    'edge_h': final_p_h - imp_h, 'edge_a': (100-final_p_h) - imp_a,
                    'oh': oh, 'oa': oa, 'h_det': h_det, 'a_det': a_det
                })

        # --- Top 3 推薦 ---
        st.divider()
        st.subheader("🔥 AI 推薦串關最優三場")
        recs = []
        for d in analysis_results:
            if d['edge_h'] > d['edge_a']: recs.append({'pick': d['h_ch'], 'edge': d['edge_h'], 'match': d['label'], 'odds': d['oh']})
            else: recs.append({'pick': d['a_ch'], 'edge': d['edge_a'], 'match': d['label'], 'odds': d['oa']})
        
        top_3 = sorted(recs, key=lambda x: x['edge'], reverse=True)[:3]
        rc1, rc2, rc3 = st.columns(3)
        for idx, r in enumerate(top_3):
            with [rc1, rc2, rc3][idx]:
                st.success(f"**No.{idx+1} {r['pick']}**\n\n價值: +{r['edge']:.1f}% | 賠率: {r['odds']}")

        # --- 單場深度分析 (v6.9 表格與分差) ---
        st.divider()
        sel_label = st.selectbox("🔍 選擇詳細數據分析", [d['label'] for d in analysis_results], key=f"sel_{i}")
        curr = next(d for d in analysis_results if d['label'] == sel_label)
        
        c1, c2, c3 = st.columns(3)
        c1.metric(curr['h_ch'], f"{curr['final_p_h']:.1f}%", f"預測分差: {curr['final_m_h']:+.1f}")
        c2.metric(curr['a_ch'], f"{100-curr['final_p_h']:.1f}%", f"預測分差: {-curr['final_m_h']:+.1f}")
        c3.metric("AI 建議贏家", curr['h_ch'] if curr['final_p_h'] > 50 else curr['a_ch'])

        # 數據表
        def get_m(m, tid, k): return maps.get(m, {}).get(int(tid), {}).get(k, 0)
        st.table(pd.DataFrame({
            "指標": ["進攻效率", "防守效率", "節奏", "轉換得分", "跑動(mi)", "傳球", "干擾投籃"],
            curr['h_ch']: [get_m('adv',curr['h_id'],'OFF_RATING'), get_m('adv',curr['h_id'],'DEF_RATING'), get_m('adv',curr['h_id'],'PACE'), get_m('trans',curr['h_id'],'PPP'), get_m('spd',curr['h_id'],'DIST_MILES'), get_m('pass',curr['h_id'],'PASSES_MADE'), get_m('hustle',curr['h_id'],'CONTESTED_SHOTS')],
            curr['a_ch']: [get_m('adv',curr['a_id'],'OFF_RATING'), get_m('adv',curr['a_id'],'DEF_RATING'), get_m('adv',curr['a_id'],'PACE'), get_m('trans',curr['a_id'],'PPP'), get_m('spd',curr['a_id'],'DIST_MILES'), get_m('pass',curr['a_id'],'PASSES_MADE'), get_m('hustle',curr['a_id'],'CONTESTED_SHOTS')]
        }))
        
        # 核心球員
        p1, p2 = st.columns(2)
        for tid, name, col in [(curr['h_id'], curr['h_ch'], p1), (curr['a_id'], curr['a_ch'], p2)]:
            with col:
                p_df = ps_full[ps_full['TEAM_ID'] == tid].sort_values('PTS', ascending=False).head(6)
                st.dataframe(p_df[['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分','TS_PCT':'真實命中%'}), hide_index=True)

st.sidebar.info(f"🕒 更新：{last_update}")
