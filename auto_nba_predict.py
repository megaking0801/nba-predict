import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats
)
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests, unicodedata
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# --- 1. 配置與語系 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')
us_east_tz = pytz.timezone('US/Eastern')

TEAM_KW = {
    'ATL': 'Hawks', 'BKN': 'Nets', 'BOS': 'Celtics', 'CHA': 'Hornets', 'CHI': 'Bulls', 'CLE': 'Cavaliers',
    'DAL': 'Mavericks', 'DEN': 'Nuggets', 'DET': 'Pistons', 'GSW': 'Warriors', 'HOU': 'Rockets', 'IND': 'Pacers',
    'LAC': 'Clippers', 'LAL': 'Lakers', 'MEM': 'Grizzlies', 'MIA': 'Heat', 'MIL': 'Bucks', 'MIN': 'Timberwolves',
    'NOP': 'Pelicans', 'NYK': 'Knicks', 'OKC': 'Thunder', 'ORL': 'Magic', 'PHI': '76ers', 'PHX': 'Suns',
    'POR': 'Blazers', 'SAC': 'Kings', 'SAS': 'Spurs', 'TOR': 'Raptors', 'UTA': 'Jazz', 'WAS': 'Wizards'
}

TEAM_NAME_CH = {
    'ATL': '老鷹', 'BKN': '籃網', 'BOS': '塞爾提克', 'CHA': '黃蜂', 'CHI': '公牛', 'CLE': '騎士',
    'DAL': '獨行俠', 'DEN': '金塊', 'DET': '活塞', 'GSW': '勇士', 'HOU': '火箭', 'IND': '溜馬',
    'LAC': '快艇', 'LAL': '湖人', 'MEM': '灰熊', 'MIA': '熱火', 'MIL': '公鹿', 'MIN': '灰狼',
    'NOP': '鵜鶘', 'NYK': '尼克', 'OKC': '雷霆', 'ORL': '魔術', 'PHI': '76人', 'PHX': '太陽',
    'POR': '拓荒者', 'SAC': '國王', 'SAS': '馬刺', 'TOR': '暴龍', 'UTA': '爵士', 'WAS': '巫師'
}

st.set_page_config(page_title="NBA 專家 v10.0", layout="wide")

# --- 2. 安全抓取引擎 ---
@st.cache_data(ttl=600)
def get_espn_injuries():
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0'}
    injury_dict = {abbr: [] for abbr in TEAM_KW.keys()}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for container in soup.select('.ResponsiveTable'):
            header = container.find_previous(class_='Table__Title')
            if not header: continue
            team_abbr = next((abbr for abbr, kw in TEAM_KW.items() if kw in header.text), None)
            if team_abbr:
                rows = container.select('tr.Table__TR')[1:]
                for row in rows:
                    tds = row.find_all('td')
                    if len(tds) >= 3:
                        injury_dict[team_abbr].append({'球員': tds[0].text.strip(), '狀態': tds[2].text.strip()})
    except: pass
    return injury_dict

def fetch_safe_df(endpoint_class, **kwargs):
    try:
        raw = endpoint_class(**kwargs).get_dict()
        res = raw['resultSets'][0] if 'resultSets' in raw else raw['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

@st.cache_data(ttl=3600)
def load_all_data():
    S = '2025-26'
    p_base = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    p_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    
    if p_base.empty: return pd.DataFrame(), pd.DataFrame(), {}, "N/A"

    p_full = pd.merge(p_base, p_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID', how='left')
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    
    t_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    nba_ids = [t['id'] for t in teams.get_teams()]
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    l10 = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(10, min_periods=3).mean()).groupby(gf['TEAM_ID']).last().to_dict()
    
    return p_full, t_adv, l10, datetime.now(tw_tz).strftime("%H:%M")

ps_db, tm_db, l10_db, update_time = load_all_data()
injuries_db = get_espn_injuries()

# --- 3. UI 介面 ---
st.title("🏀 NBA 數據專家 v10.0 (獨立讓分 + 勝率全解析版)")
st.sidebar.write(f"📊 數據更新: {update_time}")

nba_now = datetime.now(us_east_tz)
dates = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 目前無比賽資訊"); continue

        id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        analysis_results = []
        cols = st.columns(3)

        for idx, row in sb.iterrows():
            h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
            h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
            if not h_abbr or not a_abbr: continue
            
            # 取得戰力與傷病
            def get_team_stats(tid, abbr):
                inj = injuries_db.get(abbr, [])
                out_names = [p['球員'] for p in inj if any(x in p['狀態'].lower() for x in ['out', 'doubt', 'adj'])]
                active = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
                active_core = active[~active['PLAYER_NAME'].isin(out_names)].head(8)
                adv = tm_db[tm_db['TEAM_ID'] == tid].iloc[0] if not tm_db[tm_db['TEAM_ID'] == tid].empty else {}
                return {'active': active_core, 'inj': inj, 'adv': adv, 'total_imp': active_core['IMPACT'].sum()}

            h_pkg = get_team_stats(h_id, h_abbr)
            a_pkg = get_team_stats(a_id, a_abbr)

            # 勝率模型
            final_margin = (h_pkg['total_imp'] - a_pkg['total_imp']) * 0.1 + (l10_db.get(h_id, 0) - l10_db.get(a_id, 0)) * 0.5 + 2.5
            prob_h = 1 / (1 + 10**(-final_margin/15)) * 100
            
            g_key = f"{dates[i].strftime('%Y%m%d')}_{a_abbr}_{h_abbr}"
            with cols[idx % 3]:
                st.markdown(f"### {TEAM_NAME_CH[a_abbr]} @ {TEAM_NAME_CH[h_abbr]}")
                
                # --- 勝率顯示 ---
                c1, c2 = st.columns(2)
                c1.metric(f"{TEAM_NAME_CH[h_abbr]} 勝率", f"{prob_h:.1f}%")
                c2.metric(f"{TEAM_NAME_CH[a_abbr]} 勝率", f"{100-prob_h:.1f}%")
                
                # --- 獨立輸入框 ---
                oh = st.number_input(f"🏠 {TEAM_NAME_CH[h_abbr]} 賠率", value=1.85, key=f"h_{g_key}")
                oa = st.number_input(f"✈️ {TEAM_NAME_CH[a_abbr]} 賠率", value=1.85, key=f"a_{g_key}")
                sp = st.number_input(f"🚩 讓分 (給主隊 {TEAM_NAME_CH[h_abbr]})", value=0.0, key=f"s_{g_key}")
                
                analysis_results.append({
                    'label': f"{TEAM_NAME_CH[a_abbr]} @ {TEAM_NAME_CH[h_abbr]}",
                    'h_abbr': h_abbr, 'a_abbr': a_abbr, 'prob': prob_h, 'margin': final_margin,
                    'oh': oh, 'oa': oa, 'sp': sp, 'h_pkg': h_pkg, 'a_pkg': a_pkg
                })

        # --- 數據大表 ---
        if analysis_results:
            st.divider()
            sel = st.selectbox("🔍 選擇對戰查看「上場球員數據」與「全傷病表」", [x['label'] for x in analysis_results], key=f"sel_{i}")
            curr = next(x for x in analysis_results if x['label'] == sel)

            # 1. 核心數據
            st.markdown("#### 🔥 會上場核心球員數據 (已排除傷兵)")
            p1, p2 = st.columns(2)
            p1.write(f"**{curr['h_abbr']} 主力**")
            p1.dataframe(curr['h_pkg']['active'][['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT', 'IMPACT']].rename(columns={'PLAYER_NAME':'姓名'}), hide_index=True)
            p2.write(f"**{curr['a_abbr']} 主力**")
            p2.dataframe(curr['a_pkg']['active'][['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT', 'IMPACT']].rename(columns={'PLAYER_NAME':'姓名'}), hide_index=True)

            # 2. 傷病表
            st.markdown("#### 🚑 全隊傷病監控表格")
            i1, i2 = st.columns(2)
            i1.dataframe(pd.DataFrame(curr['h_pkg']['inj']) if curr['h_pkg']['inj'] else "✅ 全員健康", use_container_width=True)
            i2.dataframe(pd.DataFrame(curr['a_pkg']['inj']) if curr['a_pkg']['inj'] else "✅ 全員健康", use_container_width=True)
