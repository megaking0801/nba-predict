import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    synergyplaytypes
)
from nba_api.stats.static import teams
import pandas as pd
import numpy as np
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

st.set_page_config(page_title="NBA 數據專家 v8.5", layout="wide")

# --- 2. 工具函數 ---
def normalize_name(name):
    if not isinstance(name, str): return ""
    n = unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode("utf-8").lower()
    return n.replace('.', '').replace(' iii', '').replace(' ii', '').replace(' jr', '').strip()

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
            team_raw = section.get_text().strip()
            abbr = next((a for n, a in TEAM_NAME_EN_MAP.items() if n in team_raw), None)
            if not abbr: continue
            table = section.find_next('table')
            rows = table.find_all('tr')[1:]
            team_inj = []
            for row in rows:
                cols = row.find_all('td')
                if len(cols) >= 3:
                    name = cols[0].get_text(strip=True)
                    status_text = cols[2].get_text(strip=True).lower()
                    hard_out_keywords = ['season', 'surgery', 'torn', 'broken', 'acl', 'mcl', 'achilles', 'fracture', 'indefinitely', 'ligament', 'procedure']
                    is_season_out = any(k in status_text for k in hard_out_keywords)
                    is_out = ('out' in status_text) or is_season_out
                    is_gtd = any(k in status_text for k in ['questionable', 'doubtful', 'decision', 'probable', 'gtd']) and not is_out
                    final_status = "SEASON_OUT" if is_season_out else ("OUT" if is_out else "GTD")
                    team_inj.append({'name': name, 'status': final_status, 'raw_text': status_text.upper()})
            injury_data[abbr] = team_inj
        return injury_data
    except: return {}

# --- 3. 數據核心 (回歸 v8.0 全量數據) ---
@st.cache_data(ttl=3600)
def load_all_data_v85():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S, ST = '2025-26', 'Regular Season'
    
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST', 'MIN']], 
                        ps_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID')

    df_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_hustle = fetch_safe_df(leaguehustlestatsteam.LeagueHustleStatsTeam, season=S, per_mode_time='PerGame')
    df_spd = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
    df_pass = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='Passing', per_mode_simple='PerGame')
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
    
    def to_map(df, cols): return df.set_index('TEAM_ID')[cols].to_dict('index') if not df.empty else {}
    maps = {
        'base': to_map(df_base, ['PTS', 'REB', 'AST', 'FG_PCT']),
        'adv': to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE']),
        'hustle': to_map(df_hustle, ['DEFLECTIONS', 'CONTESTED_SHOTS']),
        'spd': to_map(df_spd, ['DIST_MILES', 'AVG_SPEED']),
        'pass': to_map(df_pass, ['PASSES_MADE']),
        'trans': to_map(df_trans, ['PPP'])
    }

    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    
    feats = ['REST_DAYS']
    clf = xgb.XGBClassifier().fit(gf[feats].fillna(0), gf['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(gf[feats].fillna(0), gf['PLUS_MINUS'].fillna(0))
    
    player_db = {normalize_name(row['PLAYER_NAME']): row.to_dict() for _, row in ps_full.iterrows()}
    return clf, reg, gf, ps_full, feats, maps, player_db, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, ps_full, feats, maps, player_db, last_update = load_all_data_v85()
injury_report = fetch_live_injuries_espn()

# --- 4. 名單與戰力分析邏輯 ---
def get_detailed_roster_v85(abbr, team_id):
    team_players = ps_full[ps_full['TEAM_ID'] == team_id].copy()
    team_players['norm_name'] = team_players['PLAYER_NAME'].apply(normalize_name)
    inj_map = {normalize_name(i['name']): i for i in injury_report.get(abbr, [])}
    
    roster_list = []
    total_power = 0
    for _, p in team_players.iterrows():
        name_norm = p['norm_name']
        status, weight, detail = "✅ 正常", 1.0, "健康"
        if name_norm in inj_map:
            inj = inj_map[name_norm]
            if inj['status'] == "SEASON_OUT": status, weight, detail = "💀 報銷", 0.0, f"嚴重: {inj['raw_text']}"
            elif inj['status'] == "OUT": status, weight, detail = "🚫 缺陣", 0.0, "不確定回歸"
            elif inj['status'] == "GTD": status, weight, detail = "⚠️ 疑慮", 0.2, f"GTD: {inj['raw_text']}"
        
        roster_list.append({'球員': p['PLAYER_NAME'], '狀態': status, '場均PTS': p['PTS'], 'TS%': p['TS_PCT'], '權重': weight, '細節': detail})
        total_power += (p['PTS'] * weight)
    return pd.DataFrame(roster_list).sort_values('場均PTS', ascending=False), total_power

# --- 5. 介面主體 ---
st.title("🏀 NBA 數據專家 v8.5 (數據/傷病大滿貫版)")

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
        game_list = []
        for _, row in sb.iterrows():
            h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
            h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
            if h_abbr and a_abbr:
                game_list.append({'label': f"{TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}", 'h_id': h_id, 'a_id': a_id, 'h_abbr': h_abbr, 'a_abbr': a_abbr})

        # A. 賠率輸入
        st.subheader("💰 當日賠率輸入")
        input_odds = {}
        o_cols = st.columns(3)
        for idx, g in enumerate(game_list):
            with o_cols[idx % 3]:
                oh = st.number_input(f"🏠 {TEAM_NAME_CH[g['h_abbr']]}", 1.75, key=f"oh_{idx}_{i}")
                oa = st.number_input(f"✈️ {TEAM_NAME_CH[g['a_abbr']]}", 1.75, key=f"oa_{idx}_{i}")
                input_odds[idx] = (oh, oa)

        # B. 核心分析與 Edge 計算
        analysis_data = []
        for idx, g in enumerate(game_list):
            h_roster, h_power = get_detailed_roster_v85(g['h_abbr'], g['h_id'])
            a_roster, a_power = get_detailed_roster_v85(g['a_abbr'], g['a_id'])
            
            h_last = gf[gf['TEAM_ABBREVIATION'] == g['h_abbr']].tail(1)
            base_p = clf.predict_proba(h_last[feats])[0][1] * 100 if not h_last.empty else 50.0
            base_m = reg.predict(h_last[feats])[0] if not h_last.empty else 0.0
            
            # 戰力修正
            power_diff = (h_power - a_power) / 5.0
            final_p_h = max(5, min(95, base_p + power_diff))
            final_m_h = base_m + (power_diff / 2.0)
            
            oh, oa = input_odds[idx]
            imp_h = (1/oh)/(1/oh + 1/oa) * 100
            edge_h, edge_a = final_p_h - imp_h, (100-final_p_h) - (100-imp_h)
            
            analysis_data.append({
                'label': g['label'], 'h_ch': TEAM_NAME_CH[g['h_abbr']], 'a_ch': TEAM_NAME_CH[g['a_abbr']],
                'final_p_h': final_p_h, 'final_m_h': final_m_h, 'edge_h': edge_h, 'edge_a': edge_a,
                'h_roster': h_roster, 'a_roster': a_roster, 'h_power': h_power, 'a_power': a_power,
                'h_id': g['h_id'], 'a_id': g['a_id'], 'odds_h': oh, 'odds_a': oa
            })

        # C. Top 3 推薦
        st.divider()
        st.subheader("🔥 AI 推薦最強三場")
        recs = []
        for d in analysis_data:
            if d['edge_h'] > d['edge_a']: recs.append({'pick': d['h_ch'], 'edge': d['edge_h'], 'match': d['label'], 'odds': d['odds_h']})
            else: recs.append({'pick': d['a_ch'], 'edge': d['edge_a'], 'match': d['label'], 'odds': d['odds_a']})
        top_3 = sorted(recs, key=lambda x: x['edge'], reverse=True)[:3]
        rc1, rc2, rc3 = st.columns(3)
        for idx, r in enumerate(top_3):
            with [rc1, rc2, rc3][idx]: st.success(f"**No.{idx+1} {r['pick']}**\n\n{r['match']}\n\n價值: +{r['edge']:.1f}% | 賠率: {r['odds']}")

        # D. 單場深度數據 (恢復 v8.0 所有表格)
        st.divider()
        sel_label = st.selectbox("🔍 選擇查看場次詳情", [d['label'] for d in analysis_data], key=f"sel_final_{i}")
        curr = next(d for d in analysis_data if d['label'] == sel_label)

        # 1. 預測卡片
        c1, c2, c3 = st.columns(3)
        c1.metric(curr['h_ch'], f"{curr['final_p_h']:.1f}%", f"預估分差: {curr['final_m_h']:+.1f}")
        c2.metric(curr['a_ch'], f"{100-curr['final_p_h']:.1f}%", f"預估分差: {-curr['final_m_h']:+.1f}")
        c3.metric("可用戰力對比", f"{curr['h_power']:.1f} vs {curr['a_power']:.1f}")

        # 2. 名單表格
        st.subheader("📋 雙方球員狀態與數據 (含上場、帶傷、報銷)")
        lc, rc = st.columns(2)
        with lc: st.dataframe(curr['h_roster'][['球員', '狀態', '場均PTS', '細節']], hide_index=True, use_container_width=True)
        with rc: st.dataframe(curr['a_roster'][['球員', '狀態', '場均PTS', '細節']], hide_index=True, use_container_width=True)

        # 3. 團隊深度指標表 (v8.0 核心)
        st.subheader("📊 團隊進階指標對比 (2025-26 賽季)")
        def get_m(m, tid, k): return maps.get(m, {}).get(int(tid), {}).get(k, 0)
        st.table(pd.DataFrame({
            "指標項目": ["進攻效率", "防守效率", "節奏 (Pace)", "轉換得分 (PPP)", "跑動(mi)", "場均傳球", "干擾投籃", "撥球"],
            curr['h_ch']: [get_m('adv',curr['h_id'],'OFF_RATING'), get_m('adv',curr['h_id'],'DEF_RATING'), get_m('adv',curr['h_id'],'PACE'), get_m('trans',curr['h_id'],'PPP'), get_m('spd',curr['h_id'],'DIST_MILES'), get_m('pass',curr['h_id'],'PASSES_MADE'), get_m('hustle',curr['h_id'],'CONTESTED_SHOTS'), get_m('hustle',curr['h_id'],'DEFLECTIONS')],
            curr['a_ch']: [get_m('adv',curr['a_id'],'OFF_RATING'), get_m('adv',curr['a_id'],'DEF_RATING'), get_m('adv',curr['a_id'],'PACE'), get_m('trans',curr['a_id'],'PPP'), get_m('spd',curr['a_id'],'DIST_MILES'), get_m('pass',curr['a_id'],'PASSES_MADE'), get_m('hustle',curr['a_id'],'CONTESTED_SHOTS'), get_m('hustle',curr['a_id'],'DEFLECTIONS')]
        }))

st.sidebar.info(f"🕒 更新：{last_update}")
st.sidebar.markdown("v8.5：已恢復所有 V8.0 團隊指標表格，並整合 Butler III 名稱修正與硬核傷病過濾。")
