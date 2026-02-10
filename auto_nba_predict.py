import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats
)
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests, re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# --- 1. 配置與中文對照 ---
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

st.set_page_config(page_title="NBA 專家 v12.0", layout="wide")

# --- 2. 雙源傷病引擎 (Rotowire 備援) ---
@st.cache_data(ttl=900)
def get_injuries_dual_source():
    """先抓官方 JSON，若失敗則抓取 Rotowire 網頁"""
    # 源 A: NBA 官方
    url_official = "https://stats.nba.com/js/data/widgets/injury_report.json"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        resp = requests.get(url_official, headers=headers, timeout=5)
        data = resp.json().get('results', [])
        if data:
            df = pd.DataFrame(data)
            df = df.rename(columns={'Player': '球員', 'Status': '狀態', 'Team': '球隊', 'Description': '說明'})
            return df[['球員', '狀態', '球隊', '說明']], "NBA官方源"
    except: pass

    # 源 B: Rotowire (備援)
    url_roto = "https://www.rotowire.com/basketball/injury-report.php"
    try:
        resp = requests.get(url_roto, headers=headers, timeout=8)
        soup = BeautifulSoup(resp.text, 'html.parser')
        rows = soup.find_all('tr', class_='is-nba')
        roto_data = []
        for r in rows:
            name = r.find('a', class_='injuries__name').text.strip() if r.find('a', class_='injuries__name') else "未知"
            team = r.find('div', class_='injuries__team').text.strip() if r.find('div', class_='injuries__team') else ""
            status = r.find('td', class_='injuries__status').text.strip() if r.find('td', class_='injuries__status') else "Out"
            desc = r.find('td', class_='injuries__comment').text.strip() if r.find('td', class_='injuries__comment') else ""
            roto_data.append({'球員': name, '狀態': status, '球隊': team, '說明': desc})
        if roto_data:
            return pd.DataFrame(roto_data), "Rotowire備援源"
    except: pass
    
    return pd.DataFrame(columns=['球員', '狀態', '球隊', '說明']), "無可用數據"

@st.cache_data(ttl=3600)
def load_master_stats():
    S = '2025-26'
    p_base = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    p_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    if p_base.empty: return pd.DataFrame(), pd.DataFrame(), {}, "N/A"
    p_full = pd.merge(p_base, p_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID', how='left')
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    t_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    gf = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    l10 = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(10, min_periods=3).mean()).groupby(gf['TEAM_ID']).last().to_dict()
    return p_full, t_adv, l10, datetime.now(tw_tz).strftime("%H:%M")

def fetch_safe_df(endpoint, **kwargs):
    try:
        r = endpoint(**kwargs).get_dict()
        res = r['resultSets'][0] if 'resultSets' in r else r['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

# --- 數據預載 ---
ps_db, tm_db, l10_db, update_time = load_master_stats()
injury_df, active_source = get_injuries_dual_source()

# --- 3. UI 顯示邏輯 ---
st.title("🏀 NBA 數據專家 v12.0 (雙源穩定分析)")
st.sidebar.info(f"當前數據源: {active_source}\n更新時間: {update_time}")

nba_now = datetime.now(us_east_tz)
dates = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 暫無比賽資訊"); continue

        id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        results = []
        cols = st.columns(3)

        for idx, row in sb.iterrows():
            h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
            h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
            if not h_abbr or not a_abbr: continue
            
            def get_team_bundle(tid, abbr):
                t_inj = injury_df[injury_df['球隊'].str.contains(abbr, na=False, case=False)]
                out_names = t_inj[t_inj['狀態'].str.contains('Out|Doubt', case=False, na=False)]['球員'].tolist()
                
                # 計算會上場的前 8 名核心球員
                all_ps = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
                active_core = all_ps[~all_ps['PLAYER_NAME'].isin(out_names)].head(8)
                
                return {
                    'pts_sum': active_core['PTS'].sum(),
                    'ts_avg': active_core['TS_PCT'].mean(),
                    'pie_avg': active_core['PIE'].mean(),
                    'impact': active_core['IMPACT'].sum(),
                    'df': active_core,
                    'inj_df': t_inj
                }

            h_m = get_team_bundle(h_id, h_abbr)
            a_m = get_team_bundle(a_id, a_abbr)

            # --- 勝率模型：上場數據加權 ---
            final_margin = (h_m['pts_sum']-a_m['pts_sum'])*0.1 + (h_m['ts_avg']-a_m['ts_avg'])*15 + (h_m['pie_avg']-a_m['pie_avg'])*40 + (l10_db.get(h_id,0)-l10_db.get(a_id,0))*0.4 + 2.5
            prob_h = 1 / (1 + 10**(-final_margin/15)) * 100
            
            h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
            g_key = f"{dates[i].strftime('%Y%m%d')}_{a_abbr}_{h_abbr}"

            with cols[idx % 3]:
                with st.container(border=True):
                    st.markdown(f"### [客隊] {a_cn} vs [主隊] {h_cn}")
                    st.metric(f"{h_cn} [主隊] 勝率", f"{prob_h:.1f}%")
                    st.metric(f"{a_cn} [客隊] 勝率", f"{100-prob_h:.1f}%")
                    
                    show_odds = st.toggle("顯示盤口輸入", key=f"tog_{g_key}")
                    if show_odds:
                        oh = st.number_input(f"{h_cn} [主隊] 賠率", value=1.85, key=f"h_{g_key}")
                        oa = st.number_input(f"{a_cn} [客隊] 賠率", value=1.85, key=f"a_{g_key}")
                        edge = (prob_h - (1/oh*100)) if prob_h > 50 else ((100-prob_h) - (1/oa*100))
                        st.info(f"💡 價值優勢: {edge:+.1f}%")

                    results.append({'label': f"[客隊] {a_cn} vs [主隊] {h_cn}", 'h_m': h_m, 'a_m': a_m, 'h_cn': h_cn, 'a_cn': a_cn})

        if results:
            st.divider()
            sel = st.selectbox("🔍 選擇對戰組合 (查看會上場數據與傷病)", [x['label'] for x in results], key=f"sel_{i}")
            curr = next(x for x in results if x['label'] == sel)

            # 1. 會上場核心
            st.markdown("#### 📊 1. 會上場球員詳細數據 (主客對照)")
            pc1, pc2 = st.columns(2)
            pc1.write(f"**[主隊] {curr['h_cn']} 核心數據**")
            pc1.dataframe(curr['h_m']['df'][['PLAYER_NAME', 'PTS', 'TS_PCT', 'PIE', 'IMPACT']].rename(columns={'PLAYER_NAME':'球員'}), hide_index=True)
            pc2.write(f"**[客隊] {curr['a_cn']} 核心數據**")
            pc2.dataframe(curr['a_m']['df'][['PLAYER_NAME', 'PTS', 'TS_PCT', 'PIE', 'IMPACT']].rename(columns={'PLAYER_NAME':'球員'}), hide_index=True)

            # 2. 傷病名單
            st.markdown(f"#### 🚑 2. 傷病來源原文 ({active_source})")
            ic1, ic2 = st.columns(2)
            with ic1:
                st.write(f"**[主隊] {curr['h_cn']} 狀態**")
                if not curr['h_m']['inj_df'].empty: st.table(curr['h_m']['inj_df'][['球員', '狀態', '說明']])
                else: st.success("✅ 目前無傷病")
            with ic2:
                st.write(f"**[客隊] {curr['a_cn']} 狀態**")
                if not curr['a_m']['inj_df'].empty: st.table(curr['a_m']['inj_df'][['球員', '狀態', '說明']])
                else: st.success("✅ 目前無傷病")
