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

# --- 1. 基本與語言設定 ---
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

st.set_page_config(page_title="NBA 數據專家 v7.9", layout="wide")

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
                    is_out = any(k in status.lower() for k in ['out', 'season', 'surgery', 'indefinitely', 'broken', 'torn'])
                    is_dqs = any(k in status.lower() for k in ['day-to-day', 'questionable', 'doubtful'])
                    team_inj.append({'name': name, 'status': status, 'is_out': is_out, 'is_dqs': is_dqs})
            injury_data[abbr] = team_inj
        return injury_data
    except: return {}

# --- 3. 數據核心 (回歸 v6.9 完整數據庫) ---
@st.cache_data(ttl=3600)
def load_all_data_v79():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S, ST = '2025-26', 'Regular Season'
    
    # A. 球員數據 (用於 Top 6 表格與 PPG 匹配)
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST']], ps_adv[['PLAYER_ID', 'TS_PCT']], on='PLAYER_ID')
    player_stats_db = {normalize_name(row['PLAYER_NAME']): row['PTS'] for _, row in ps_full.iterrows()}

    # B. 團隊進階數據 (v6.9 所有分類)
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

    # C. 模型訓練 (勝率)
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    
    feats = ['REST_DAYS']
    clf = xgb.XGBClassifier().fit(gf[feats].fillna(0), gf['WIN_BIN'])
    
    return clf, gf, ps_full, feats, maps, player_stats_db, datetime.now(tw_tz).strftime("%H:%M")

clf, gf, ps_full, feats, maps, player_stats_db, last_update = load_all_data_v79()
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
        details.append(f"{icon} {inj['name']} [{inj['status']}] (場均{ppg:.1f}分) -{final_p:.1f}%")
    return score, details

# --- 4. 介面呈現 ---
st.title("🏀 NBA 數據專家 v7.9 (全數據回歸 + 串關神器)")
st.sidebar.caption(f"🕒 更新時間：{last_update}")

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

        # --- 第一部分：串關賠率輸入 ---
        st.subheader("💰 當日賠率批次輸入")
        with st.expander("展開輸入運彩賠率", expanded=True):
            input_odds = {}
            o_cols = st.columns(3)
            for idx, g in enumerate(game_list):
                with o_cols[idx % 3]:
                    st.write(f"**{g['label']}**")
                    oh = st.number_input(f"🏠 {TEAM_NAME_CH.get(g['h_abbr'])}", value=1.75, step=0.01, key=f"oh_{i}_{idx}")
                    oa = st.number_input(f"✈️ {TEAM_NAME_CH.get(g['a_abbr'])}", value=1.75, step=0.01, key=f"oa_{i}_{idx}")
                    input_odds[idx] = (oh, oa)

        # --- 第二部分：AI 推薦 Top 3 ---
        recommendations = []
        for idx, g in enumerate(game_list):
            h_last = gf[gf['TEAM_ABBREVIATION'] == g['h_abbr']].tail(1)
            base_p = clf.predict_proba(h_last[feats])[0][1] * 100 if not h_last.empty else 50.0
            h_imp, _ = get_injury_summary(g['h_abbr'], player_stats_db)
            a_imp, _ = get_injury_summary(g['a_abbr'], player_stats_db)
            ai_h = max(5, min(95, base_p - h_imp + a_imp))
            ai_a = 100 - ai_h
            
            oh, oa = input_odds[idx]
            imp_h = (1/oh)/(1/oh + 1/oa) * 100
            imp_a = (1/oa)/(1/oh + 1/oa) * 100
            
            if ai_h - imp_h > ai_a - imp_a:
                recommendations.append({'match': g['label'], 'pick': TEAM_NAME_CH.get(g['h_abbr']), 'edge': ai_h - imp_h, 'odds': oh})
            else:
                recommendations.append({'match': g['label'], 'pick': TEAM_NAME_CH.get(g['a_abbr']), 'edge': ai_a - imp_a, 'odds': oa})

        top_3 = sorted(recommendations, key=lambda x: x['edge'], reverse=True)[:3]
        st.divider()
        st.subheader("🔥 AI 推薦串關組合 (Top 3 價值排名)")
        rc1, rc2, rc3 = st.columns(3)
        for idx, r in enumerate(top_3):
            with [rc1, rc2, rc3][idx]:
                st.info(f"**No.{idx+1} {r['pick']}**")
                st.write(f"對決: {r['match']}")
                st.write(f"AI 信心領先莊家: +{r['edge']:.1f}%")
                st.write(f"賠率: {r['odds']}")

        # --- 第三部分：單場深度數據 (回歸 v6.9 表格) ---
        st.divider()
        sel_label = st.selectbox("🔍 查看詳細數據與核心球員", [g['label'] for g in game_list], key=f"sel_{i}")
        curr = next(g for g in game_list if g['label'] == sel_label)
        h_id, a_id = curr['h_id'], curr['a_id']
        h_ch, a_ch = TEAM_NAME_CH.get(curr['h_abbr']), TEAM_NAME_CH.get(curr['a_abbr'])

        # 🚑 傷病名單
        st.subheader("🚑 即時傷病名單 (ESPN)")
        ic1, ic2 = st.columns(2)
        _, h_det = get_injury_summary(curr['h_abbr'], player_stats_db)
        _, a_det = get_injury_summary(curr['a_abbr'], player_stats_db)
        with ic1:
            st.write(f"**{h_ch}**")
            if h_det: 
                for d in h_det: st.write(d)
            else: st.success("目前無傷病")
        with ic2:
            st.write(f"**{a_ch}**")
            if a_det: 
                for d in a_det: st.write(d)
            else: st.success("目前無傷病")

        # 📊 v6.9 核心團隊數據表
        def get_m(m, tid, k): return maps.get(m, {}).get(int(tid), {}).get(k, 0)
        st.subheader(f"📊 {sel_label} 團隊數據深度對比")
        st.table(pd.DataFrame({
            "指標分類": ["進攻效率 (OffRtg)", "防守效率 (DefRtg)", "比賽節奏 (Pace)", "轉換得分 (PPP)", "跑動英里 (Dist)", "平均速度 (Spd)", "場均傳球 (Pass)", "干擾投籃 (Contest)", "撥球次數 (Deflect)"],
            h_ch: [get_m('adv',h_id,'OFF_RATING'), get_m('adv',h_id,'DEF_RATING'), get_m('adv',h_id,'PACE'), get_m('trans',h_id,'PPP'), get_m('spd',h_id,'DIST_MILES'), get_m('spd',h_id,'AVG_SPEED'), get_m('pass',h_id,'PASSES_MADE'), get_m('hustle',h_id,'CONTESTED_SHOTS'), get_m('hustle',h_id,'DEFLECTIONS')],
            a_ch: [get_m('adv',a_id,'OFF_RATING'), get_m('adv',a_id,'DEF_RATING'), get_m('adv',a_id,'PACE'), get_m('trans',a_id,'PPP'), get_m('spd',a_id,'DIST_MILES'), get_m('spd',a_id,'AVG_SPEED'), get_m('pass',a_id,'PASSES_MADE'), get_m('hustle',a_id,'CONTESTED_SHOTS'), get_m('hustle',a_id,'DEFLECTIONS')]
        }))

        # 🚀 v6.9 核心球員表
        st.subheader(f"🚀 {h_ch} vs {a_ch} 核心球員名單")
        p1, p2 = st.columns(2)
        for tid, name, col in [(h_id, h_ch, p1), (a_id, a_ch, p2)]:
            with col:
                st.write(f"**{name}**")
                p_df = ps_full[ps_full['TEAM_ID'] == tid].sort_values('PTS', ascending=False).head(6)
                st.dataframe(p_df[['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分','TS_PCT':'真實命中%'}), hide_index=True)
