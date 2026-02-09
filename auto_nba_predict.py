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
    'NOP': '紐奧良推土機', 'NYK': '紐約尼克', 'OKC': '奧克拉荷馬雷霆',
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
                    status = cols[2].get_text(strip=True)
                    # 判斷是否確定缺陣
                    is_out = any(k in status.lower() for k in ['out', 'season', 'surgery', 'indefinitely', 'broken', 'torn'])
                    team_inj.append({'name': name, 'status': status, 'is_out': is_out})
            injury_data[abbr] = team_inj
        return injury_data
    except: return {}

# --- 3. 數據核心 (包含會上場球員分析) ---
@st.cache_data(ttl=3600)
def load_all_data_v81():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S, ST = '2025-26', 'Regular Season'
    
    # 1. 抓取所有球員詳細數據
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(
        ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST', 'MIN']], 
        ps_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], 
        on='PLAYER_ID'
    )
    player_stats_db = {normalize_name(row['PLAYER_NAME']): row.to_dict() for _, row in ps_full.iterrows()}

    # 2. 團隊數據地圖
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

    # 3. 模型訓練
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    
    feats = ['REST_DAYS']
    clf = xgb.XGBClassifier().fit(gf[feats].fillna(0), gf['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(gf[feats].fillna(0), gf['PLUS_MINUS'].fillna(0))
    
    return clf, reg, gf, ps_full, feats, maps, player_stats_db, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, ps_full, feats, maps, player_stats_db, last_update = load_all_data_v81()
injury_report = fetch_live_injuries_espn()

# --- 4. 核心分析邏輯：區分傷兵與上場球員 ---
def analyze_roster(abbr, team_id, db, ps_df):
    injuries = injury_report.get(abbr, [])
    out_names = [normalize_name(i['name']) for i in injuries if i['is_out']]
    warn_names = [normalize_name(i['name']) for i in injuries if not i['is_out']]
    
    # 1. 獲取該隊所有球員數據
    team_players = ps_df[ps_df['TEAM_ID'] == team_id].copy()
    team_players['norm_name'] = team_players['PLAYER_NAME'].apply(normalize_name)
    
    # 2. 分類：誰會打，誰不打
    active_players = team_players[~team_players['norm_name'].isin(out_names)].sort_values('PTS', ascending=False)
    out_players = team_players[team_players['norm_name'].isin(out_names)]
    
    # 3. 計算戰力損失與現有戰力
    penalty_score = 0
    injury_details = []
    for _, row in out_players.iterrows():
        ppg = row['PTS']
        penalty = 12 if ppg >= 25 else (7 if ppg >= 18 else (3 if ppg >= 10 else 1))
        penalty_score += penalty
        injury_details.append(f"❌ {row['PLAYER_NAME']} (缺陣) - 損失場均 {ppg:.1f}分")
        
    for name in warn_names:
        if name in team_players['norm_name'].values:
            p_name = team_players[team_players['norm_name'] == name]['PLAYER_NAME'].values[0]
            injury_details.append(f"⚠️ {p_name} (疑慮/賽前決定)")

    # 4. 現有戰力指標 (Active Roster Stats)
    active_top_8 = active_players.head(8)
    active_ppg = active_top_8['PTS'].sum()
    active_pie = active_top_8['PIE'].mean()
    
    return penalty_score, injury_details, active_players.head(10), active_ppg, active_pie

# --- 5. 介面主體 ---
st.title("🏀 NBA 數據專家 v8.1 (全方位上場球員分析版)")

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
        st.subheader("💰 賠率與市場期望值")
        input_odds = {}
        o_cols = st.columns(3)
        for idx, g in enumerate(game_list):
            with o_cols[idx % 3]:
                st.write(f"**{g['label']}**")
                oh = st.number_input(f"🏠 {TEAM_NAME_CH.get(g['h_abbr'])}", value=1.75, step=0.01, key=f"oh_{idx}_{i}")
                oa = st.number_input(f"✈️ {TEAM_NAME_CH.get(g['a_abbr'])}", value=1.75, step=0.01, key=f"oa_{idx}_{i}")
                input_odds[idx] = (oh, oa)

        # B. 深度分析 (整合上場球員)
        analysis_results = []
        for idx, g in enumerate(game_list):
            # 獲取主客隊名單分析
            h_pen, h_inj_det, h_active, h_appg, h_apie = analyze_roster(g['h_abbr'], g['h_id'], player_stats_db, ps_full)
            a_pen, a_inj_det, a_active, a_appg, a_apie = analyze_roster(g['a_abbr'], g['a_id'], player_stats_db, ps_full)
            
            # 模型基礎預測
            h_last = gf[gf['TEAM_ABBREVIATION'] == g['h_abbr']].tail(1)
            base_p = clf.predict_proba(h_last[feats])[0][1] * 100 if not h_last.empty else 50.0
            base_m = reg.predict(h_last[feats])[0] if not h_last.empty else 0.0
            
            # 修正：考量傷病扣分與現有球員戰力比
            # 這裡我們加入對比 active_ppg (上場球員總得分能力)
            power_diff = (h_appg - a_appg) / 5.0
            final_p_h = max(5, min(95, base_p - h_pen + a_pen + power_diff))
            final_m_h = base_m - (h_pen/3) + (a_pen/3) + (power_diff/2)
            
            oh, oa = input_odds[idx]
            imp_h = (1/oh)/(1/oh + 1/oa) * 100
            edge_h = final_p_h - imp_h
            edge_a = (100-final_p_h) - ((1/oa)/(1/oh + 1/oa) * 100)
            
            analysis_results.append({
                'label': g['label'], 'h_ch': TEAM_NAME_CH.get(g['h_abbr']), 'a_ch': TEAM_NAME_CH.get(g['a_abbr']),
                'final_p_h': final_p_h, 'final_m_h': final_m_h, 'edge_h': edge_h, 'edge_a': edge_a,
                'h_active': h_active, 'a_active': a_active, 'h_inj': h_inj_det, 'a_inj': a_inj_det,
                'h_id': g['h_id'], 'a_id': g['a_id'], 'h_abbr': g['h_abbr'], 'a_abbr': g['a_abbr'],
                'odds_h': oh, 'odds_a': oa
            })

        # C. AI 推薦
        st.divider()
        st.subheader("🔥 AI 推薦 (結合會上場球員戰力分析)")
        recs = []
        for d in analysis_results:
            pick = d['h_ch'] if d['edge_h'] > d['edge_a'] else d['a_ch']
            edge = max(d['edge_h'], d['edge_a'])
            odds = d['odds_h'] if d['edge_h'] > d['edge_a'] else d['odds_a']
            recs.append({'pick': pick, 'edge': edge, 'match': d['label'], 'odds': odds})
        
        top_3 = sorted(recs, key=lambda x: x['edge'], reverse=True)[:3]
        rc1, rc2, rc3 = st.columns(3)
        for idx, r in enumerate(top_3):
            with [rc1, rc2, rc3][idx]:
                st.success(f"**No.{idx+1} {r['pick']}**\n\n{r['match']}\n\n優勢: +{r['edge']:.1f}% | 賠率: {r['odds']}")

        # D. 單場深度：會上場球員 vs 傷兵
        st.divider()
        sel_label = st.selectbox("🔍 選擇場次查看「上場名單」與「詳細數據」", [d['label'] for d in analysis_results], key=f"sel_v81_{i}")
        curr = next(d for d in analysis_results if d['label'] == sel_label)

        # 核心數據卡片
        cc1, cc2, cc3 = st.columns(3)
        cc1.metric(curr['h_ch'], f"{curr['final_p_h']:.1f}%", f"預估分差: {curr['final_m_h']:+.1f}")
        cc2.metric(curr['a_ch'], f"{100-curr['final_p_h']:.1f}%", f"預估分差: {-curr['final_m_h']:+.1f}")
        cc3.metric("AI 建議強邊", curr['h_ch'] if curr['final_p_h'] > 50 else curr['a_ch'])

        # 名單對決
        st.write("---")
        lc, rc = st.columns(2)
        with lc:
            st.subheader(f"🏠 {curr['h_ch']} 戰力配置")
            st.warning("⚠️ 傷病/缺陣名單")
            for det in curr['h_inj']: st.write(f"- {det}")
            if not curr['h_inj']: st.success("- 全員待命")
            
            st.info("✅ 今日預計出賽核心 (Top 10)")
            st.dataframe(curr['h_active'][['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT', 'MIN']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分','TS_PCT':'真實命中%'}), hide_index=True)

        with rc:
            st.subheader(f"✈️ {curr['a_ch']} 戰力配置")
            st.warning("⚠️ 傷病/缺陣名單")
            for det in curr['a_inj']: st.write(f"- {det}")
            if not curr['a_inj']: st.success("- 全員待命")
            
            st.info("✅ 今日預計出賽核心 (Top 10)")
            st.dataframe(curr['a_active'][['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT', 'MIN']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分','TS_PCT':'真實命中%'}), hide_index=True)

        # 團隊深度數據表格 (保留原本強大的數據對比)
        def get_m(m, tid, k): return maps.get(m, {}).get(int(tid), {}).get(k, 0)
        st.subheader("📊 團隊進階指標對比")
        st.table(pd.DataFrame({
            "指標項目": ["進攻效率", "防守效率", "節奏", "轉換得分 (PPP)", "跑動距離(mi)", "場均傳球", "干擾投籃", "撥球(Deflections)"],
            curr['h_ch']: [get_m('adv',curr['h_id'],'OFF_RATING'), get_m('adv',curr['h_id'],'DEF_RATING'), get_m('adv',curr['h_id'],'PACE'), get_m('trans',curr['h_id'],'PPP'), get_m('spd',curr['h_id'],'DIST_MILES'), get_m('pass',curr['h_id'],'PASSES_MADE'), get_m('hustle',curr['h_id'],'CONTESTED_SHOTS'), get_m('hustle',curr['h_id'],'DEFLECTIONS')],
            curr['a_ch']: [get_m('adv',curr['a_id'],'OFF_RATING'), get_m('adv',curr['a_id'],'DEF_RATING'), get_m('adv',curr['a_id'],'PACE'), get_m('trans',curr['a_id'],'PPP'), get_m('spd',curr['a_id'],'DIST_MILES'), get_m('pass',curr['a_id'],'PASSES_MADE'), get_m('hustle',curr['a_id'],'CONTESTED_SHOTS'), get_m('hustle',curr['a_id'],'DEFLECTIONS')]
        }))

st.sidebar.info(f"🕒 系統更新：{last_update}")
st.sidebar.markdown("""
**v8.1 更新說明：**
1. **動態名單過濾**：自動將 ESPN 顯示為 'Out' 的球員從戰力評估中移除。
2. **上場球員分析**：顯示每隊預計出賽的前 10 名核心，並計算其賽季場均得分與上場時間。
3. **戰力修正**：勝率預測模型現在會根據「可用球員」的總分與賽季平均的落差進行微調。
""")
