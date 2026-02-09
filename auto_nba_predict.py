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
import numpy as np
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
    'Atlanta Hawks': 'ATL', 'Brooklyn Nets': 'BKN', 'Boston Celtics': 'BOS',
    'Charlotte Hornets': 'CHA', 'Chicago Bulls': 'CHI', 'Cleveland Cavaliers': 'CLE',
    'Dallas Mavericks': 'DAL', 'Denver Nuggets': 'DEN', 'Detroit Pistons': 'DET',
    'Golden State Warriors': 'GSW', 'Houston Rockets': 'HOU', 'Indiana Pacers': 'IND',
    'Los Angeles Clippers': 'LAC', 'L.A. Clippers': 'LAC', 'Los Angeles Lakers': 'LAL', 'L.A. Lakers': 'LAL',
    'Memphis Grizzlies': 'MEM', 'Miami Heat': 'MIA', 'Milwaukee Bucks': 'MIL',
    'Minnesota Timberwolves': 'MIN', 'New Orleans Pelicans': 'NOP', 'New York Knicks': 'NYK',
    'Oklahoma City Thunder': 'OKC', 'Orlando Magic': 'ORL', 'Philadelphia 76ers': 'PHI',
    'Phoenix Suns': 'PHX', 'Portland Trail Blazers': 'POR', 'Sacramento Kings': 'SAC',
    'San Antonio Spurs': 'SAS', 'Toronto Raptors': 'TOR', 'Utah Jazz': 'UTA', 'Washington Wizards': 'WAS'
}

st.set_page_config(page_title="NBA 數據專家 v7.2", layout="wide")
st.title("🏀 NBA 數據專家 v7.2 (傷病權重 & 完整數據融合版)")

# --- 2. 穩定抓取與工具函數 ---
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
        elif 'ID' in df.columns: df['TEAM_ID'] = df['ID'].astype(int)
        return df
    except: return pd.DataFrame()

@st.cache_data(ttl=600)
def fetch_live_injuries():
    url = "https://www.cbssports.com/nba/injuries/"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        injury_data = {}
        for section in soup.find_all('div', class_='TeamLogoNameLockup-name'):
            team_abbr = TEAM_NAME_EN_MAP.get(section.get_text().strip())
            if not team_abbr: continue
            rows = section.find_next('table').find_all('tr')[1:]
            team_injuries = []
            for row in rows:
                cols = row.find_all('td')
                if len(cols) >= 3:
                    team_injuries.append({
                        'name': cols[0].get_text(strip=True),
                        'status': cols[2].get_text(strip=True),
                        'is_out': any(x in cols[2].get_text(strip=True).lower() for x in ['out', 'injured', 'susp']),
                        'is_dqs': any(x in cols[2].get_text(strip=True).lower() for x in ['day-to-day', 'questionable', 'doubtful'])
                    })
            injury_data[team_abbr] = team_injuries
        return injury_data
    except: return {}

@st.cache_data(ttl=3600)
def load_all_data_v72():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S, ST = '2025-26', 'Regular Season'
    
    # 抓取所有 v6.9 所需的 Maps 數據
    df_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_hustle = fetch_safe_df(leaguehustlestatsteam.LeagueHustleStatsTeam, season=S, per_mode_time='PerGame')
    df_track_spd = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
    df_track_pass = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='Passing', per_mode_simple='PerGame')
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
    df_iso = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Isolation', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
    df_rim = fetch_safe_df(leaguedashptdefend.LeagueDashPtDefend, season=S, defense_category='Less Than 6 Ft', season_type_all_star=ST)

    def to_map(df, cols): return df.set_index('TEAM_ID')[cols].to_dict('index') if not df.empty else {}
    maps = {
        'base': to_map(df_base, ['PTS', 'REB', 'AST', 'FG_PCT']),
        'adv': to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE']),
        'hustle': to_map(df_hustle, ['DEFLECTIONS', 'CONTESTED_SHOTS']),
        'spd': to_map(df_track_spd, ['DIST_MILES', 'AVG_SPEED']),
        'pass': to_map(df_track_pass, ['PASSES_MADE']),
        'trans': to_map(df_trans, ['PPP']),
        'iso': to_map(df_iso, ['PPP']),
        'rim': to_map(df_rim, ['D_FG_PCT'])
    }

    # 訓練模型 (加入 Rest Days)
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    
    feats = ['REST_DAYS'] # 基礎特徵
    train_df = gf.fillna(0)
    clf = xgb.XGBClassifier().fit(train_df[feats], train_df['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(train_df[feats], train_df['PLUS_MINUS'])
    
    # 球員數據用於傷病權重
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST']], ps_adv[['PLAYER_ID', 'TS_PCT']], on='PLAYER_ID')
    
    player_stats_db = {normalize_name(row['PLAYER_NAME']): row['PTS'] for _, row in ps_full.iterrows()}
    
    return clf, reg, gf, ps_full, feats, maps, player_stats_db, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, ps_full, feats, maps, player_stats_db, last_update = load_all_data_v72()
injury_report = fetch_live_injuries()

# --- 3. 傷病衝擊計算邏輯 ---
def get_injury_impact(injuries, db):
    score, details = 0, []
    for inj in injuries:
        ppg = db.get(normalize_name(inj['name']), 0)
        weight = 1.0 if inj['is_out'] else (0.5 if inj['is_dqs'] else 0)
        penalty = 0
        if ppg >= 25: penalty = 12
        elif ppg >= 18: penalty = 7
        elif ppg >= 10: penalty = 3
        elif ppg >= 5: penalty = 1
        
        final_p = penalty * weight
        if final_p > 0:
            score += final_p
            details.append(f"{'❌' if inj['is_out'] else '⚠️'} {inj['name']} ({ppg:.1f}分) -{final_p:.1f}%")
    return score, details

# --- 4. 介面顯示 ---
nba_now = datetime.now(us_east_tz)
dates_nba = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1), nba_now - timedelta(days=2)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates_nba])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates_nba[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 暫無賽程數據")
            continue

        id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        games_list = []
        for _, row in sb.iterrows():
            h_abbr, a_abbr = id_to_abbr.get(row['HOME_TEAM_ID']), id_to_abbr.get(row['VISITOR_TEAM_ID'])
            if h_abbr and a_abbr:
                games_list.append(f"{TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}")

        if games_list:
            selected = st.selectbox("🎯 選擇分析場次", games_list, key=f"s_{i}")
            # 解析選擇的球隊
            a_ch, h_ch = selected.split(" @ ")
            h_abbr = [k for k, v in TEAM_NAME_CH.items() if v == h_ch][0]
            a_abbr = [k for k, v in TEAM_NAME_CH.items() if v == a_ch][0]
            h_id = [t['id'] for t in teams.get_teams() if t['abbreviation'] == h_abbr][0]
            a_id = [t['id'] for t in teams.get_teams() if t['abbreviation'] == a_abbr][0]

            # 1. 計算勝率與傷病修正
            h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
            base_prob = clf.predict_proba(h_last[feats])[0][1] * 100 if not h_last.empty else 50.0
            
            h_impact, h_details = get_injury_impact(injury_report.get(h_abbr, []), player_stats_db)
            a_impact, a_details = get_injury_impact(injury_report.get(a_abbr, []), player_stats_db)
            
            final_prob = max(5, min(95, base_prob - h_impact + a_impact))
            diff_pred = round(abs(float(reg.predict(h_last[feats])[0])), 1) if not h_last.empty else 0

            # 2. 預測大卡片
            st.markdown(f"### 🏟️ {selected}")
            c1, c2, c3 = st.columns(3)
            c1.metric(h_ch, f"{final_prob:.1f}%", f"傷病修正: -{h_impact:.1f}%")
            c2.metric(a_ch, f"{100-final_prob:.1f}%", f"傷病修正: -{a_impact:.1f}%")
            winner = h_ch if final_prob > 50 else a_ch
            c3.metric("AI 最終預測贏家", winner, f"預估分差: {diff_pred}")

            # 3. 傷病名單標註區 (新加入)
            st.subheader("🚑 確定未出戰 / 傷病名單")
            ic1, ic2 = st.columns(2)
            with ic1:
                st.write(f"**{h_ch}**")
                if h_details:
                    for d in h_details: st.error(d)
                else: st.success("目前無重大傷病回報")
            with ic2:
                st.write(f"**{a_ch}**")
                if a_details:
                    for d in a_details: st.error(d)
                else: st.success("目前無重大傷病回報")

            # 4. 原始 v6.9 數據表格
            def get_m(m, tid, k): return maps[m].get(int(tid), {}).get(k, 0)
            
            st.divider()
            st.subheader("📊 1. 團隊場均基礎數據")
            st.table(pd.DataFrame({
                "指標項目": ["場均得分", "場均籃板", "場均助攻", "團隊命中率", "進攻效率", "防守效率", "比賽節奏"],
                h_ch: [f"{get_m('base', h_id, 'PTS'):.1f}", f"{get_m('base', h_id, 'REB'):.1f}", f"{get_m('base', h_id, 'AST'):.1f}", f"{get_m('base', h_id, 'FG_PCT'):.1%}", f"{get_m('adv', h_id, 'OFF_RATING')}", f"{get_m('adv', h_id, 'DEF_RATING')}", f"{get_m('adv', h_id, 'PACE')}"],
                a_ch: [f"{get_m('base', a_id, 'PTS'):.1f}", f"{get_m('base', a_id, 'REB'):.1f}", f"{get_m('base', a_id, 'AST'):.1f}", f"{get_m('base', a_id, 'FG_PCT'):.1%}", f"{get_m('adv', a_id, 'OFF_RATING')}", f"{get_m('adv', a_id, 'DEF_RATING')}", f"{get_m('adv', a_id, 'PACE')}"]
            }))

            st.subheader("🏃‍♂️ 2. 體能與積極度 (場均)")
            st.table(pd.DataFrame({
                "指標": ["撥球破壞", "干擾投籃", "跑動里程", "移動速度", "傳球次數"],
                h_ch: [f"{get_m('hustle', h_id, 'DEFLECTIONS'):.1f}", f"{get_m('hustle', h_id, 'CONTESTED_SHOTS'):.1f}", f"{get_m('spd', h_id, 'DIST_MILES'):.2f} mi", f"{get_m('spd', h_id, 'AVG_SPEED'):.2f} mph", f"{get_m('pass', h_id, 'PASSES_MADE'):.1f}"],
                a_ch: [f"{get_m('hustle', a_id, 'DEFLECTIONS'):.1f}", f"{get_m('hustle', a_id, 'CONTESTED_SHOTS'):.1f}", f"{get_m('spd', a_id, 'DIST_MILES'):.2f} mi", f"{get_m('spd', a_id, 'AVG_SPEED'):.2f} mph", f"{get_m('pass', a_id, 'PASSES_MADE'):.1f}"]
            }))

            st.subheader("🚀 3. 核心球員數據 (Top 6)")
            for tid, name in [(h_id, f"🏠 {h_ch}"), (a_id, f"✈️ {a_ch}")]:
                st.write(f"**{name}**")
                p_df = ps_full[ps_full['TEAM_ID'] == tid].sort_values('PTS', ascending=False).head(6)
                st.dataframe(p_df[['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分','REB':'籃板','AST':'助攻','TS_PCT':'真實命中%'}).style.format({'得分':'{:.1f}','籃板':'{:.1f}','助攻':'{:.1f}','真實命中%':'{:.1%}'}), hide_index=True)

st.sidebar.caption(f"🕒 更新時間：{last_update}")
st.sidebar.info("傷病來源: CBS Sports Live Feed")
