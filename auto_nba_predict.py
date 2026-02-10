import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats
)
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests
from datetime import datetime, timedelta

# --- 1. 核心配置 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')
us_east_tz = pytz.timezone('US/Eastern')

TEAM_NAME_CH = {
    'ATL': '老鷹', 'BKN': '籃網', 'BOS': '塞爾提克', 'CHA': '黃蜂', 'CHI': '公牛', 'CLE': '騎士',
    'DAL': '獨行俠', 'DEN': '金塊', 'DET': '活塞', 'GSW': '勇士', 'HOU': '火箭', 'IND': '溜馬',
    'LAC': '快艇', 'LAL': '湖人', 'MEM': '灰熊', 'MIA': '熱火', 'MIL': '公鹿', 'MIN': '灰狼',
    'NOP': '鵜鶘', 'NYK': '尼克', 'OKC': '雷霆', 'ORL': '魔術', 'PHI': '76人', 'PHX': '太陽',
    'POR': '拓荒者', 'SAC': '國王', 'SAS': '馬刺', 'TOR': '暴龍', 'UTA': 'Jazz', 'WAS': 'Wizards'
}

st.set_page_config(page_title="NBA 專家 v11.1", layout="wide")

# --- 2. 數據抓取引擎 ---
def fetch_safe_df(endpoint_class, **kwargs):
    try:
        raw = endpoint_class(**kwargs).get_dict()
        res = raw['resultSets'][0] if 'resultSets' in raw else raw['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

@st.cache_data(ttl=1800)
def get_official_injuries():
    """從 NBA 官方 Widget 接口獲取傷病數據"""
    url = "https://stats.nba.com/js/data/widgets/injury_report.json"
    try:
        headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.nba.com/'}
        resp = requests.get(url, headers=headers, timeout=10)
        return resp.json().get('results', [])
    except: return []

@st.cache_data(ttl=3600)
def load_master_data():
    S = '2025-26'
    p_base = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    p_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    
    if p_base.empty: return pd.DataFrame(), pd.DataFrame(), {}, "N/A"

    p_full = pd.merge(p_base, p_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID', how='left')
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    
    t_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    l10 = gf_raw.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(10, min_periods=3).mean()).groupby(gf_raw['TEAM_ID']).last().to_dict()
    
    return p_full, t_adv, l10, datetime.now(tw_tz).strftime("%H:%M")

ps_db, tm_db, l10_db, update_time = load_master_data()
official_injuries = get_official_injuries()

# --- 3. UI 邏輯 ---
st.title("🏀 NBA 數據專家 v11.1 (官方源穩定整合版)")
st.sidebar.markdown(f"**數據更新: {update_time}**")

nba_now = datetime.now(us_east_tz)
dates = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 官方今日暫無排程"); continue

        id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        results = []
        cols = st.columns(3)

        for idx, row in sb.iterrows():
            h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
            h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
            if not h_abbr or not a_abbr: continue
            
            # --- 數據計算邏輯：加入預計上場球員全數據 ---
            def get_team_bundle(tid, abbr):
                # 官方傷病篩選
                team_inj = [p for p in official_injuries if abbr in str(p.get('Team', ''))]
                out_names = [p.get('Player') for p in team_inj if 'Out' in str(p.get('Status', ''))]
                
                # 篩選預計上場的前 8 名核心球員
                all_ps = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
                active_core = all_ps[~all_ps['PLAYER_NAME'].isin(out_names)].head(8)
                
                return {
                    'pts_sum': active_core['PTS'].sum(),
                    'ts_avg': active_core['TS_PCT'].mean(),
                    'pie_avg': active_core['PIE'].mean(),
                    'imp_sum': active_core['IMPACT'].sum(),
                    'df': active_core,
                    'inj_df': pd.DataFrame(team_inj) if team_inj else pd.DataFrame(columns=['Player','Status','Description']),
                    'adv': tm_db[tm_db['TEAM_ID'] == tid].iloc[0].to_dict() if not tm_db[tm_db['TEAM_ID'] == tid].empty else {}
                }

            h_m = get_team_bundle(h_id, h_abbr)
            a_m = get_team_bundle(a_id, a_abbr)

            # 勝率複合模型：上場球員數據 (PTS, TS%, PIE) + 戰力 (Impact) + L10
            pts_factor = (h_m['pts_sum'] - a_m['pts_sum']) * 0.12
            eff_factor = (h_m['ts_avg'] - a_m['ts_avg']) * 15 + (h_m['pie_avg'] - a_m['pie_avg']) * 40
            trend_factor = (l10_db.get(h_id, 0) - l10_db.get(a_id, 0)) * 0.4
            
            final_margin = pts_factor + eff_factor + trend_factor + 2.5
            prob_h = 1 / (1 + 10**(-final_margin/15)) * 100
            
            g_key = f"{dates[i].strftime('%Y%m%d')}_{a_abbr}_{h_abbr}"
            with cols[idx % 3]:
                with st.container(border=True):
                    st.markdown(f"#### {TEAM_NAME_CH.get(a_abbr, a_abbr)} @ {TEAM_NAME_CH.get(h_abbr, h_abbr)}")
                    st.metric("🏠 主隊勝率", f"{prob_h:.1f}%", f"{final_margin:+.1f} 分差")
                    
                    show_odds = st.toggle("盤口模式", key=f"tog_{g_key}")
                    if show_odds:
                        oh = st.number_input(f"🏠 賠率", value=1.85, key=f"h_{g_key}")
                        oa = st.number_input(f"✈️ 賠率", value=1.85, key=f"a_{g_key}")
                        sp = st.number_input(f"🚩 讓分", value=0.0, key=f"s_{g_key}")
                        edge = (prob_h - (1/oh*100)) if prob_h > 50 else ((100-prob_h) - (1/oa*100))
                        st.success(f"價值優勢: {edge:+.1f}%")

                    results.append({'label': f"{a_abbr} @ {h_abbr}", 'h_m': h_m, 'a_m': a_m, 'h_abbr': h_abbr, 'a_abbr': a_abbr})

        # --- 底部數據大表 ---
        if results:
            st.divider()
            sel = st.selectbox("🔍 選擇對戰組合查看詳細官方數據", [x['label'] for x in results], key=f"sel_{i}")
            curr = next(x for x in results if x['label'] == sel)

            st.markdown("### 📊 1. 會上場核心數據 (參與勝率計算)")
            pc1, pc2 = st.columns(2)
            pc1.write(f"**{curr['h_abbr']} 預計主力**")
            pc1.dataframe(curr['h_m']['df'][['PLAYER_NAME', 'PTS', 'TS_PCT', 'PIE', 'IMPACT']], hide_index=True)
            pc2.write(f"**{curr['a_abbr']} 預計主力**")
            pc2.dataframe(curr['a_m']['df'][['PLAYER_NAME', 'PTS', 'TS_PCT', 'PIE', 'IMPACT']], hide_index=True)

            st.markdown("### 🚑 2. 官方傷病狀態原文")
            ic1, ic2 = st.columns(2)
            with ic1:
                st.write(f"**{curr['h_abbr']} 官方通報**")
                if not curr['h_m']['inj_df'].empty: st.table(curr['h_m']['inj_df'])
                else: st.info("✅ 官方目前無傷病通報")
            with ic2:
                st.write(f"**{curr['a_abbr']} 官方通報**")
                if not curr['a_m']['inj_df'].empty: st.table(curr['a_m']['inj_df'])
                else: st.info("✅ 官方目前無傷病通報")
