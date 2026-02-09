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

st.set_page_config(page_title="NBA 數據專家 v7.8", layout="wide")

# --- 2. 工具函數 ---
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
        sections = soup.find_all(class_='Table__Title')
        for section in sections:
            team_name_raw = section.get_text().strip()
            abbr = next((a for n, a in TEAM_NAME_EN_MAP.items() if n in team_name_raw), None)
            if not abbr: continue
            table = section.find_next('table')
            rows = table.find_all('tr')[1:]
            team_inj = []
            for row in rows:
                cols = row.find_all('td')
                if len(cols) >= 3:
                    name = cols[0].get_text(strip=True); status = cols[2].get_text(strip=True)
                    is_out = any(k in status.lower() for k in ['out', 'season', 'surgery', 'indefinitely', 'broken', 'torn'])
                    is_dqs = any(k in status.lower() for k in ['day-to-day', 'questionable', 'doubtful'])
                    team_inj.append({'name': name, 'status': status, 'is_out': is_out, 'is_dqs': is_dqs})
            injury_data[abbr] = team_inj
        return injury_data
    except: return {}

# --- 3. 數據核心 (穩定版) ---
@st.cache_data(ttl=3600)
def load_all_data_v78():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S, ST = '2025-26', 'Regular Season'
    
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST']], ps_adv[['PLAYER_ID', 'TS_PCT']], on='PLAYER_ID')
    player_stats_db = {normalize_name(row['PLAYER_NAME']): row['PTS'] for _, row in ps_full.iterrows()}

    df_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_hustle = fetch_safe_df(leaguehustlestatsteam.LeagueHustleStatsTeam, season=S, per_mode_time='PerGame')
    df_track_spd = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
    df_track_pass = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='Passing', per_mode_simple='PerGame')
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
    
    def to_map(df, cols): return df.set_index('TEAM_ID')[cols].to_dict('index') if not df.empty else {}
    maps = {
        'base': to_map(df_base, ['PTS', 'REB', 'AST', 'FG_PCT']),
        'adv': to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE']),
        'hustle': to_map(df_hustle, ['DEFLECTIONS', 'CONTESTED_SHOTS']),
        'spd': to_map(df_track_spd, ['DIST_MILES', 'AVG_SPEED']),
        'pass': to_map(df_track_pass, ['PASSES_MADE']),
        'trans': to_map(df_trans, ['PPP'])
    }

    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    
    feats = ['REST_DAYS']
    clf = xgb.XGBClassifier().fit(gf[feats].fillna(0), gf['WIN_BIN'])
    
    return clf, gf, ps_full, feats, maps, player_stats_db, datetime.now(tw_tz).strftime("%H:%M")

clf, gf, ps_full, feats, maps, player_stats_db, last_update = load_all_data_v78()
injury_report = fetch_live_injuries_espn()

def get_injury_summary(abbr, db):
    injuries = injury_report.get(abbr, [])
    score, details = 0, []
    for inj in injuries:
        ppg = db.get(normalize_name(inj['name']), 0)
        weight = 1.0 if inj['is_out'] else 0.5
        penalty = 12 if ppg >= 25 else (7 if ppg >= 18 else (3 if ppg >= 10 else (1 if ppg >= 5 else 0)))
        final_p = penalty * weight
        score += final_p
        icon = "❌" if inj['is_out'] else "⚠️"
        details.append(f"{icon} {inj['name']} (場均{ppg:.1f})")
    return score, details

# --- 4. 介面主體 ---
st.title("🏀 NBA 數據專家 v7.8 (串關優化與價值發現)")

nba_now = datetime.now(us_east_tz)
dates_nba = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates_nba])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates_nba[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 暫無賽程數據")
            continue

        id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        analysis_results = []

        # 第一步：計算當天所有比賽的 AI 預測數據
        for _, row in sb.iterrows():
            h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
            h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
            if not h_abbr or not a_abbr: continue
            
            h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
            ai_prob_h = clf.predict_proba(h_last[feats])[0][1] * 100 if not h_last.empty else 50.0
            h_impact, h_details = get_injury_summary(h_abbr, player_stats_db)
            a_impact, a_details = get_injury_summary(a_abbr, player_stats_db)
            
            final_ai_h = max(5, min(95, ai_prob_h - h_impact + a_impact))
            final_ai_a = 100 - final_ai_h
            
            analysis_results.append({
                'label': f"{TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}",
                'h_abbr': h_abbr, 'a_abbr': a_abbr, 'h_id': h_id, 'a_id': a_id,
                'ai_h': final_ai_h, 'ai_a': final_ai_a,
                'h_details': h_details, 'a_details': a_details
            })

        # 第二步：批次賠率輸入
        st.subheader("💰 批次賠率輸入 (請填入運彩賠率)")
        with st.expander("展開輸入當日賠率", expanded=True):
            input_odds = {}
            cols = st.columns(3) # 每行放三場
            for idx, res in enumerate(analysis_results):
                with cols[idx % 3]:
                    st.write(f"**{res['label']}**")
                    oh = st.number_input(f"🏠 {TEAM_NAME_CH.get(res['h_abbr'])}", value=1.75, step=0.01, key=f"h_{i}_{idx}")
                    oa = st.number_input(f"✈️ {TEAM_NAME_CH.get(res['a_abbr'])}", value=1.75, step=0.01, key=f"a_{i}_{idx}")
                    input_odds[idx] = (oh, oa)

        # 第三步：串關推薦算法 (Edge = AI Prob - Implied Prob)
        recommendations = []
        for idx, res in enumerate(analysis_results):
            oh, oa = input_odds[idx]
            # 轉換為隱含機率 (排除莊家水份)
            implied_h = (1/oh) / (1/oh + 1/oa) * 100
            implied_a = (1/oa) / (1/oh + 1/oa) * 100
            
            edge_h = res['ai_h'] - implied_h
            edge_a = res['ai_a'] - implied_a
            
            if edge_h > edge_a:
                recommendations.append({'match': res['label'], 'pick': TEAM_NAME_CH.get(res['h_abbr']), 'edge': edge_h, 'odds': oh})
            else:
                recommendations.append({'match': res['label'], 'pick': TEAM_NAME_CH.get(res['a_abbr']), 'edge': edge_a, 'odds': oa})

        # 排序並選出前三名
        top_3 = sorted(recommendations, key=lambda x: x['edge'], reverse=True)[:3]

        st.divider()
        st.subheader("🔥 AI 串關最優三場建議 (信心度排序)")
        rc1, rc2, rc3 = st.columns(3)
        for idx, r in enumerate(top_3):
            with [rc1, rc2, rc3][idx]:
                st.info(f"**Top {idx+1}: {r['pick']}**")
                st.write(f"賽事: {r['match']}")
                st.write(f"價值差距: +{r['edge']:.1f}%")
                st.write(f"當前賠率: {r['odds']}")

        # 第四步：單場深度分析 (原有功能保留，供細看)
        st.divider()
        sel_label = st.selectbox("🔍 查閱單場詳細數據與傷病", [r['label'] for r in analysis_results], key=f"sel_{i}")
        curr = next(r for r in analysis_results if r['label'] == sel_label)
        
        sc1, sc2 = st.columns(2)
        with sc1:
            st.write(f"**{TEAM_NAME_CH.get(curr['h_abbr'])} 傷病**")
            for d in curr['h_details']: st.write(d)
        with sc2:
            st.write(f"**{TEAM_NAME_CH.get(curr['a_abbr'])} 傷病**")
            for d in curr['a_details']: st.write(d)

        # 數據對照表
        def get_m(m, tid, k): return maps.get(m, {}).get(int(tid), {}).get(k, 0)
        st.table(pd.DataFrame({
            "指標": ["進攻效率", "防守效率", "節奏", "傳球數"],
            TEAM_NAME_CH.get(curr['h_abbr']): [get_m('adv',curr['h_id'],'OFF_RATING'), get_m('adv',curr['h_id'],'DEF_RATING'), get_m('adv',curr['h_id'],'PACE'), get_m('pass',curr['h_id'],'PASSES_MADE')],
            TEAM_NAME_CH.get(curr['a_abbr']): [get_m('adv',curr['a_id'],'OFF_RATING'), get_m('adv',curr['a_id'],'DEF_RATING'), get_m('adv',curr['a_id'],'PACE'), get_m('pass',curr['a_id'],'PASSES_MADE')]
        }))

st.sidebar.write(f"🕒 數據更新時間: {last_update}")
