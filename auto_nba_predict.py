import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, commonteamroster
)
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests, unicodedata
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# --- 1. 核心配置與語系 ---
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

st.set_page_config(page_title="NBA 數據專家 v9.9", layout="wide")

# --- 2. 數據抓取引擎 ---
@st.cache_data(ttl=900)
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

def fetch_nba_api(endpoint_class, **kwargs):
    try:
        raw = endpoint_class(**kwargs).get_dict()
        res = raw['resultSets'][0] if 'resultSets' in raw else raw['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

@st.cache_data(ttl=3600)
def load_master_data():
    S = '2025-26'
    # 1. 球員所有數據 (基礎 + 進階)
    p_base = fetch_nba_api(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    p_adv = fetch_nba_api(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    p_full = pd.merge(p_base[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST', 'STL', 'BLK', 'TOV']], 
                     p_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID')
    # 計算戰力影響力 (Impact Score)
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    
    # 2. 團隊進階數據
    t_adv = fetch_nba_api(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    
    # 3. 近期戰績 (L10)
    gf = fetch_nba_api(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    nba_ids = [t['id'] for t in teams.get_teams()]
    gf = gf[gf['TEAM_ID'].isin(nba_ids)].copy()
    l10 = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(10, min_periods=3).mean()).groupby(gf['TEAM_ID']).last().to_dict()
    
    return p_full, t_adv, l10, datetime.now(tw_tz).strftime("%H:%M")

ps_db, tm_db, l10_db, update_time = load_master_data()
injuries_db = get_espn_injuries()

# --- 3. 主介面與邏輯 ---
st.title("🏀 NBA 數據專家 v9.9 - 終極數據整合版")
st.sidebar.markdown(f"### 🕒 數據更新時間\n`{update_time}`")

if st.sidebar.button("🔄 強制刷新數據"):
    st.cache_data.clear()
    st.rerun()

nba_now = datetime.now(us_east_tz)
game_dates = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in game_dates])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_nba_api(scoreboardv2.ScoreboardV2, game_date=game_dates[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 此日期暫無比賽資訊"); continue

        id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        analysis_data = []
        card_cols = st.columns(3)
        
        for idx, row in sb.iterrows():
            h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
            h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
            if not h_abbr or not a_abbr: continue
            
            # --- 數據處理：會上場球員 vs 傷病 ---
            def process_team(tid, abbr):
                inj_list = injuries_db.get(abbr, [])
                out_names = [p['球員'] for p in inj_list if any(x in p['狀態'].lower() for x in ['out', 'doubt', 'adj'])]
                
                all_players = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
                active_players = all_players[~all_players['PLAYER_NAME'].isin(out_names)].head(10) # 顯示前10名核心
                
                adv = tm_db[tm_db['TEAM_ID'] == tid].iloc[0] if not tm_db[tm_db['TEAM_ID'] == tid].empty else {}
                return {'active': active_players, 'injuries': inj_list, 'adv': adv, 'impact_sum': active_players['IMPACT'].sum()}

            h_pkg = process_team(h_id, h_abbr)
            a_pkg = process_team(a_id, a_abbr)
            
            # 計算勝率 (L10趨勢 + 核心戰力差 + 進階指標)
            diff_impact = (h_pkg['impact_sum'] - a_pkg['impact_sum']) * 0.1
            diff_l10 = (l10_db.get(h_id, 0) - l10_db.get(a_id, 0)) * 0.5
            pred_margin = diff_impact + diff_l10 + 2.5
            win_prob = 1 / (1 + 10**(-pred_margin/15)) * 100
            
            g_key = f"{game_dates[i].strftime('%Y%m%d')}_{a_abbr}_{h_abbr}"
            with card_cols[idx % 3]:
                st.subheader(f"{TEAM_NAME_CH[a_abbr]} @ {TEAM_NAME_CH[h_abbr]}")
                oh = st.number_input(f"🏠 賠率", value=1.85, key=f"h_{g_key}")
                oa = st.number_input(f"✈️ 賠率", value=1.85, key=f"a_{g_key}")
                sp = st.number_input(f"🚩 讓分", value=0.0, key=f"s_{g_key}")
                
                analysis_data.append({
                    'label': f"{TEAM_NAME_CH[a_abbr]} @ {TEAM_NAME_CH[h_abbr]}",
                    'h_abbr': h_abbr, 'a_abbr': a_abbr, 'prob': win_prob, 'margin': pred_margin,
                    'oh': oh, 'oa': oa, 'sp': sp, 'h_pkg': h_pkg, 'a_pkg': a_pkg
                })

        # --- 價值推薦 ---
        st.divider()
        st.subheader("🔥 AI 價值分析推薦")
        if analysis_data:
            recs = []
            for d in analysis_data:
                # 獨贏價值
                edge = (d['prob'] - (1/d['oh']*100)) if d['prob'] > 50 else ((100-d['prob']) - (1/d['oa']*100))
                pick = TEAM_NAME_CH[d['h_abbr']] if d['prob'] > 50 else TEAM_NAME_CH[d['a_abbr']]
                recs.append({'pick': pick, 'edge': edge, 'match': d['label']})
            
            for r in sorted(recs, key=lambda x: x['edge'], reverse=True)[:3]:
                st.success(f"**推薦：{r['pick']}** ({r['match']}) - 價值優勢：{r['edge']:.1f}%")

        # --- 終極詳細表格 ---
        st.divider()
        if analysis_data:
            sel_game = st.selectbox("🔍 選擇組合查看完整數據 (含所有球員、傷病、進階指標)", [x['label'] for x in analysis_data], key=f"sel_{i}")
            curr = next(x for x in analysis_data if x['label'] == sel_game)
            
            # 表格1: 進階數據對比
            st.markdown("#### 1️⃣ 團隊進階效率對比")
            t_comp = pd.DataFrame([
                {"球隊": curr['h_abbr'], "進攻效率": curr['h_pkg']['adv'].get('E_OFF_RATING'), "防守效率": curr['h_pkg']['adv'].get('E_DEF_RATING'), "淨效率": curr['h_pkg']['adv'].get('E_NET_RATING'), "節奏": curr['h_pkg']['adv'].get('PACE')},
                {"球隊": curr['a_abbr'], "進攻效率": curr['a_pkg']['adv'].get('E_OFF_RATING'), "防守效率": curr['a_pkg']['adv'].get('E_DEF_RATING'), "淨效率": curr['a_pkg']['adv'].get('E_NET_RATING'), "節奏": curr['a_pkg']['adv'].get('PACE')}
            ])
            st.table(t_comp)

            # 表格2: 傷病名單
            st.markdown("#### 2️⃣ 完整傷病名單表格")
            i_col1, i_col2 = st.columns(2)
            with i_col1:
                st.write(f"**{curr['h_abbr']} 傷情**")
                st.dataframe(pd.DataFrame(curr['h_pkg']['injuries']) if curr['h_pkg']['injuries'] else "✅ 全員健康", use_container_width=True)
            with i_col2:
                st.write(f"**{curr['a_abbr']} 傷情**")
                st.dataframe(pd.DataFrame(curr['a_pkg']['injuries']) if curr['a_pkg']['injuries'] else "✅ 全員健康", use_container_width=True)

            # 表格3: 會上場球員數據 (核心 10 人)
            st.markdown("#### 3️⃣ 會上場球員詳細數據 (Active Core)")
            p_col1, p_col2 = st.columns(2)
            with p_col1:
                st.write(f"**{curr['h_abbr']} 上場核心**")
                st.dataframe(curr['h_pkg']['active'][['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT', 'PIE', 'IMPACT']].rename(columns={'PLAYER_NAME':'姓名','TS_PCT':'命中%'}), hide_index=True)
            with p_col2:
                st.write(f"**{curr['a_abbr']} 上場核心**")
                st.dataframe(curr['a_pkg']['active'][['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT', 'PIE', 'IMPACT']].rename(columns={'PLAYER_NAME':'姓名','TS_PCT':'命中%'}), hide_index=True)
