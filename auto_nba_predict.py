import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats
)
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# --- 1. 配置 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')
us_east_tz = pytz.timezone('US/Eastern')

TEAM_NAME_CH = {
    'ATL': '老鷹', 'BKN': '籃網', 'BOS': '塞爾提克', 'CHA': '黃蜂', 'CHI': '公牛', 'CLE': '騎士',
    'DAL': '獨行俠', 'DEN': '金塊', 'DET': '活塞', 'GSW': '勇士', 'HOU': '火箭', 'IND': '溜馬',
    'LAC': '快艇', 'LAL': '湖人', 'MEM': '灰熊', 'MIA': '熱火', 'MIL': '公鹿', 'MIN': '灰狼',
    'NOP': '鵜鶘', 'NYK': '尼克', 'OKC': '雷霆', 'ORL': '魔術', 'PHI': '76人', 'PHX': '太陽',
    'POR': '拓荒者', 'SAC': '國王', 'SAS': '馬刺', 'TOR': '暴龍', 'UTA': '爵士', 'WAS': '巫師'
}

TEAM_KW = {k: teams.find_team_by_abbreviation(k)['full_name'].split()[-1] for k in TEAM_NAME_CH.keys()}

st.set_page_config(page_title="NBA 專家 v10.6", layout="wide")

# --- 2. 數據引擎 (包含球員詳細數據計算) ---
@st.cache_data(ttl=600)
def get_espn_injuries():
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0'}
    injury_dict = {abbr: [] for abbr in TEAM_NAME_CH.keys()}
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
    
    if p_base.empty or p_adv.empty: return pd.DataFrame(), pd.DataFrame(), {}, "N/A"

    p_full = pd.merge(p_base, p_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID', how='left')
    # IMPACT 定義
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    
    t_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    nba_ids = [t['id'] for t in teams.get_teams()]
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    l10 = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(10, min_periods=3).mean()).groupby(gf['TEAM_ID']).last().to_dict()
    
    return p_full, t_adv, l10, datetime.now(tw_tz).strftime("%H:%M")

ps_db, tm_db, l10_db, update_time = load_all_data()
injuries_db = get_espn_injuries()

# --- 3. UI 與計算邏輯 ---
st.title("🏀 NBA 數據專家 v10.6 (全量球員數據加權版)")

nba_now = datetime.now(us_east_tz)
dates = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 目前無比賽資訊"); continue

        id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        results = []
        cols = st.columns(3)

        for idx, row in sb.iterrows():
            h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
            h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
            if not h_abbr or not a_abbr: continue
            
            # --- 核心數據計算：把「會上場球員」的 PTS, TS%, PIE 全部拿進來 ---
            def get_active_metrics(tid, abbr):
                inj = injuries_db.get(abbr, [])
                out_names = [p['球員'] for p in inj if any(x in p['狀態'].lower() for x in ['out', 'doubt', 'adj'])]
                
                # 篩選會上場的核心 (前8人)
                active = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
                active_core = active[~active['PLAYER_NAME'].isin(out_names)].head(8)
                
                metrics = {
                    'pts_total': active_core['PTS'].sum(),
                    'avg_ts': active_core['TS_PCT'].mean(),
                    'avg_pie': active_core['PIE'].mean(),
                    'total_impact': active_core['IMPACT'].sum(),
                    'count': len(active_core),
                    'df': active_core,
                    'inj': inj
                }
                adv_row = tm_db[tm_db['TEAM_ID'] == tid]
                metrics['team_adv'] = adv_row.iloc[0].to_dict() if not adv_row.empty else {}
                return metrics

            h_m = get_active_metrics(h_id, h_abbr)
            a_m = get_active_metrics(a_id, a_abbr)

            # --- 全數據複合模型計算 ---
            # 1. 基礎 Impact 差
            impact_diff = (h_m['total_impact'] - a_m['total_impact']) * 0.05
            # 2. 預期得分差 (火力)
            pts_diff = (h_m['pts_total'] - a_m['pts_total']) * 0.2
            # 3. 效率修正 (TS% 和 PIE)
            eff_diff = ((h_m['avg_ts'] - a_m['avg_ts']) * 10 + (h_m['avg_pie'] - a_m['avg_pie']) * 50)
            # 4. 趨勢
            trend_diff = (l10_db.get(h_id, 0) - l10_db.get(a_id, 0)) * 0.4
            
            final_margin = impact_diff + pts_diff + eff_diff + trend_diff + 2.3 # 2.3 為主場基礎分
            prob_h = 1 / (1 + 10**(-final_margin/15)) * 100
            
            g_key = f"{dates[i].strftime('%Y%m%d')}_{a_abbr}_{h_abbr}"
            with cols[idx % 3]:
                with st.container(border=True):
                    st.markdown(f"### {TEAM_NAME_CH[a_abbr]} @ {TEAM_NAME_CH[h_abbr]}")
                    
                    c1, c2 = st.columns(2)
                    c1.metric(f"🏠 {h_abbr}", f"{prob_h:.1f}%")
                    c2.metric(f"✈️ {a_abbr}", f"{100-prob_h:.1f}%")
                    st.caption(f"📊 綜合火力差: {pts_diff:+.1f} | 效率修正: {eff_diff:+.1f}")

                    show_odds = st.toggle("盤口模式", key=f"tog_{g_key}")
                    if show_odds:
                        oh = st.number_input(f"🏠 賠率", value=1.85, key=f"h_{g_key}")
                        oa = st.number_input(f"✈️ 賠率", value=1.85, key=f"a_{g_key}")
                        sp = st.number_input(f"🚩 讓分", value=0.0, key=f"s_{g_key}")
                        edge = (prob_h - (1/oh*100)) if prob_h > 50 else ((100-prob_h) - (1/oa*100))
                        st.info(f"💡 價值優勢: {edge:+.1f}%")
                    
                    results.append({'label': f"{TEAM_NAME_CH[a_abbr]} @ {TEAM_NAME_CH[h_abbr]}", 'h_m': h_m, 'a_m': a_m, 'h_abbr': h_abbr, 'a_abbr': a_abbr})

        # --- 下方數據表 (全部都要) ---
        if results:
            st.divider()
            sel = st.selectbox("🔍 深度查看數據對比", [x['label'] for x in results], key=f"sel_{i}")
            curr = next(x for x in results if x['label'] == sel)

            st.markdown("#### 1️⃣ 會上場球員詳細數據 (計算核心)")
            p1, p2 = st.columns(2)
            p1.write(f"**{curr['h_abbr']} (可用核心 PTS: {curr['h_m']['pts_total']:.1f})**")
            p1.dataframe(curr['h_m']['df'][['PLAYER_NAME', 'PTS', 'TS_PCT', 'PIE', 'IMPACT']], hide_index=True)
            p2.write(f"**{curr['a_abbr']} (可用核心 PTS: {curr['a_m']['pts_total']:.1f})**")
            p2.dataframe(curr['a_m']['df'][['PLAYER_NAME', 'PTS', 'TS_PCT', 'PIE', 'IMPACT']], hide_index=True)

            st.markdown("#### 2️⃣ 完整傷病與團隊進階指標")
            i1, i2 = st.columns(2)
            i1.write(f"**{curr['h_abbr']} 傷病/進階**")
            i1.dataframe(pd.DataFrame(curr['h_m']['inj']), use_container_width=True)
            i2.write(f"**{curr['a_abbr']} 傷病/進階**")
            i2.dataframe(pd.DataFrame(curr['a_m']['inj']), use_container_width=True)
