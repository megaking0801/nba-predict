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

st.set_page_config(page_title="NBA 數據專家 v7.7", layout="wide")

# --- 2. 工具與爬蟲 ---
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
                    name = cols[0].get_text(strip=True)
                    status = cols[2].get_text(strip=True)
                    is_out = any(k in status.lower() for k in ['out', 'season', 'surgery', 'indefinitely', 'broken', 'torn'])
                    is_dqs = any(k in status.lower() for k in ['day-to-day', 'questionable', 'doubtful'])
                    team_inj.append({'name': name, 'status': status, 'is_out': is_out, 'is_dqs': is_dqs})
            injury_data[abbr] = team_inj
        return injury_data
    except: return {}

# --- 3. 數據核心 (徹底修復 KeyError) ---
@st.cache_data(ttl=3600)
def load_all_data_v77():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S, ST = '2025-26', 'Regular Season'
    
    # 球員基礎數據
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST']], ps_adv[['PLAYER_ID', 'TS_PCT']], on='PLAYER_ID')
    player_stats_db = {normalize_name(row['PLAYER_NAME']): row['PTS'] for _, row in ps_full.iterrows()}

    # 團隊深度數據
    df_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_hustle = fetch_safe_df(leaguehustlestatsteam.LeagueHustleStatsTeam, season=S, per_mode_time='PerGame')
    df_track_spd = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
    df_track_pass = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='Passing', per_mode_simple='PerGame')
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
    
    def to_map(df, cols): return df.set_index('TEAM_ID')[cols].to_dict('index') if not df.empty else {}
    
    # 建立 Maps (確保包含所有表格會呼叫的 Key)
    maps = {
        'base': to_map(df_base, ['PTS', 'REB', 'AST', 'FG_PCT']),
        'adv': to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE']),
        'hustle': to_map(df_hustle, ['DEFLECTIONS', 'CONTESTED_SHOTS']),
        'spd': to_map(df_track_spd, ['DIST_MILES', 'AVG_SPEED']),
        'pass': to_map(df_track_pass, ['PASSES_MADE']),
        'trans': to_map(df_trans, ['PPP'])
    }

    # 模型訓練
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

clf, reg, gf, ps_full, feats, maps, player_stats_db, last_update = load_all_data_v77()
injury_report = fetch_live_injuries_espn()

# --- 4. 運算邏輯 ---
def get_injury_summary(injuries, db):
    score, details = 0, []
    for inj in injuries:
        ppg = db.get(normalize_name(inj['name']), 0)
        weight = 1.0 if inj['is_out'] else 0.5
        penalty = 12 if ppg >= 25 else (7 if ppg >= 18 else (3 if ppg >= 10 else (1 if ppg >= 5 else 0)))
        final_p = penalty * weight
        score += final_p
        icon = "❌" if inj['is_out'] else "⚠️"
        impact_txt = f"-{final_p:.1f}%" if final_p > 0 else "(影響輕微)"
        details.append(f"{icon} {inj['name']} [{inj['status']}] (場均{ppg:.1f}分) {impact_txt}")
    return score, details

# --- 5. 介面呈現 ---
st.title("🏀 NBA 數據專家 v7.7 (KeyError 修復版)")
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
        game_options = {}
        for _, row in sb.iterrows():
            h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
            h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
            if h_abbr and a_abbr:
                label = f"{TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}"
                game_options[label] = (h_id, a_id, h_abbr, a_abbr)

        if game_options:
            selected_game = st.selectbox("🎯 選擇分析場次", list(game_options.keys()), key=f"sel_{i}")
            h_id, a_id, h_abbr, a_abbr = game_options[selected_game]
            h_ch, a_ch = TEAM_NAME_CH.get(h_abbr), TEAM_NAME_CH.get(a_abbr)

            # A. 勝率預測
            h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
            base_prob = clf.predict_proba(h_last[feats])[0][1] * 100 if not h_last.empty else 50.0
            h_impact, h_details = get_injury_summary(injury_report.get(h_abbr, []), player_stats_db)
            a_impact, a_details = get_injury_summary(injury_report.get(a_abbr, []), player_stats_db)
            final_prob = max(5, min(95, base_prob - h_impact + a_impact))

            st.markdown(f"### 🏟️ {selected_game}")
            c1, c2, c3 = st.columns(3)
            c1.metric(h_ch, f"{final_prob:.1f}%", f"傷病修正 -{h_impact:.1f}%")
            c2.metric(a_ch, f"{100-final_prob:.1f}%", f"傷病修正 -{a_impact:.1f}%")
            c3.metric("AI 推薦贏家", h_ch if final_prob > 50 else a_ch)

            # B. 運彩盤口價值分析
            st.divider()
            st.subheader("💰 台灣運彩 / 盤口價值對照")
            oc1, oc2 = st.columns(2)
            h_odds = oc1.number_input(f"🏠 {h_ch} 賠率", value=1.75, step=0.01, key=f"ho_{i}")
            a_odds = oc2.number_input(f"✈️ {a_ch} 賠率", value=1.75, step=0.01, key=f"ao_{i}")
            raw_h = 1/h_odds; raw_a = 1/a_odds
            implied_h = (raw_h / (raw_h + raw_a)) * 100
            diff = final_prob - implied_h
            if abs(diff) > 5: st.success(f"🔥 AI 發現價值！比莊家多出 {abs(diff):.1f}% 信心看好 {'主隊' if diff > 0 else '客隊'}")
            else: st.warning("⚖️ AI 與盤口看法接近，無顯著投注價值。")

            # C. 傷病細節
            st.subheader("🚑 即時傷病名單 (含報銷/小兵)")
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

            # D. v6.9 經典數據表 (修正保險邏輯)
            def get_m(m, tid, k): 
                # 增加保險：如果 m 不在 maps 裡，返回 0
                category = maps.get(m, {})
                return category.get(int(tid), {}).get(k, 0)
                
            st.divider()
            st.subheader("📊 團隊場均數據")
            st.table(pd.DataFrame({
                "指標項目": ["得分", "進攻效率", "防守效率", "節奏", "跑動英里", "傳球數"],
                h_ch: [f"{get_m('base',h_id,'PTS'):.1f}", get_m('adv',h_id,'OFF_RATING'), get_m('adv',h_id,'DEF_RATING'), get_m('adv',h_id,'PACE'), get_m('spd',h_id,'DIST_MILES'), get_m('pass',h_id,'PASSES_MADE')],
                a_ch: [f"{get_m('base',a_id,'PTS'):.1f}", get_m('adv',a_id,'OFF_RATING'), get_m('adv',a_id,'DEF_RATING'), get_m('adv',a_id,'PACE'), get_m('spd',a_id,'DIST_MILES'), get_m('pass',a_id,'PASSES_MADE')]
            }))

            st.subheader("🚀 核心球員 Top 6")
            for tid, name in [(h_id, h_ch), (a_id, a_ch)]:
                st.write(f"**{name}**")
                p_df = ps_full[ps_full['TEAM_ID'] == tid].sort_values('PTS', ascending=False).head(6)
                st.dataframe(p_df[['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分','TS_PCT':'真實命中%'}), hide_index=True)

st.sidebar.caption(f"🕒 更新：{last_update}")
