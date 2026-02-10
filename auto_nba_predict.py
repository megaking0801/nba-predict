import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats
)
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests
from datetime import datetime, timedelta

# --- 1. 配置與中文對照表 ---
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

st.set_page_config(page_title="NBA 專家 v11.5", layout="wide")

# --- 2. 數據抓取引擎 (強化官方傷病) ---
def fetch_safe_df(endpoint_class, **kwargs):
    try:
        raw = endpoint_class(**kwargs).get_dict()
        res = raw['resultSets'][0] if 'resultSets' in raw else raw['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

@st.cache_data(ttl=900)
def get_official_injuries_stable():
    """抓取 NBA 官方即時傷病報告"""
    url = "https://stats.nba.com/js/data/widgets/injury_report.json"
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.nba.com/'}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json().get('results', [])
        # 轉換為 DataFrame 方便處理
        df = pd.DataFrame(data)
        if not df.empty:
            # 統一欄位名稱方便後續使用
            df = df.rename(columns={'Player': '球員', 'Status': '狀態', 'Team': '球隊', 'Description': '說明'})
        return df
    except: return pd.DataFrame()

@st.cache_data(ttl=3600)
def load_all_master_data():
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

ps_db, tm_db, l10_db, update_time = load_all_master_data()
injury_df_all = get_official_injuries_stable()

# --- 3. UI 與對戰解析 ---
st.title("🏀 NBA 數據專家 v11.5 (官方源 + 全中文化版)")
st.sidebar.write(f"📊 最後同步: {update_time}")

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
            
            # --- 數據包：包含上場球員 & 官方傷病 ---
            def build_team_package(tid, abbr):
                # 從官方 DF 中過濾球隊
                t_inj = injury_df_all[injury_df_all['球隊'].str.contains(abbr, na=False)] if not injury_df_all.empty else pd.DataFrame()
                out_names = t_inj[t_inj['狀態'].str.contains('Out', case=False, na=False)]['球員'].tolist()
                
                # 計算會上場的核心 (前8人)
                active = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
                active_core = active[~active['PLAYER_NAME'].isin(out_names)].head(8)
                
                return {
                    'pts': active_core['PTS'].sum(),
                    'ts': active_core['TS_PCT'].mean(),
                    'pie': active_core['PIE'].mean(),
                    'impact': active_core['IMPACT'].sum(),
                    'df': active_core,
                    'inj_df': t_inj
                }

            h_pkg = build_team_package(h_id, h_abbr)
            a_pkg = build_team_package(a_id, a_abbr)

            # --- 勝率模型 (全量球員數據參與) ---
            final_margin = (h_pkg['pts'] - a_pkg['pts'])*0.1 + (h_pkg['ts'] - a_pkg['ts'])*10 + (h_pkg['pie'] - a_pkg['pie'])*40 + (l10_db.get(h_id,0) - l10_db.get(a_id,0))*0.5 + 2.5
            prob_h = 1 / (1 + 10**(-final_margin/15)) * 100
            
            g_key = f"{dates[i].strftime('%Y%m%d')}_{a_abbr}_{h_abbr}"
            h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
            
            with cols[idx % 3]:
                with st.container(border=True):
                    st.markdown(f"### {a_cn} @ {h_cn}")
                    c1, c2 = st.columns(2)
                    c1.metric(f"🏠 {h_cn}", f"{prob_h:.1f}%")
                    c2.metric(f"✈️ {a_cn}", f"{100-prob_h:.1f}%")
                    
                    show_odds = st.toggle("盤口模式", key=f"tog_{g_key}")
                    if show_odds:
                        oh = st.number_input(f"🏠 {h_cn} 賠率", value=1.85, key=f"h_{g_key}")
                        oa = st.number_input(f"✈️ {a_cn} 賠率", value=1.85, key=f"a_{g_key}")
                        sp = st.number_input(f"🚩 讓分", value=0.0, key=f"s_{g_key}")
                        edge = (prob_h - (1/oh*100)) if prob_h > 50 else ((100-prob_h) - (1/oa*100))
                        st.success(f"價值優勢: {edge:+.1f}%")
                    
                    analysis_results.append({
                        'label': f"✈️ {a_cn} (客) vs 🏠 {h_cn} (主)",
                        'h_pkg': h_pkg, 'a_pkg': a_pkg, 'h_cn': h_cn, 'a_cn': a_cn
                    })

        # --- 底部詳細數據大表 ---
        if analysis_results:
            st.divider()
            sel = st.selectbox("🔍 選擇組合 (查看會上場數據與官方傷病)", [x['label'] for x in analysis_results], key=f"sel_{i}")
            curr = next(x for x in analysis_results if x['label'] == sel)

            # 表格1: 會上場球員數據
            st.markdown("#### 📊 1. 會上場核心數據 (參與勝率分析)")
            pc1, pc2 = st.columns(2)
            with pc1:
                st.write(f"🏠 **主隊 {curr['h_cn']}**")
                st.dataframe(curr['h_pkg']['df'][['PLAYER_NAME', 'PTS', 'TS_PCT', 'PIE', 'IMPACT']].rename(columns={'PLAYER_NAME':'球員'}), hide_index=True)
            with pc2:
                st.write(f"✈️ **客隊 {curr['a_cn']}**")
                st.dataframe(curr['a_pkg']['df'][['PLAYER_NAME', 'PTS', 'TS_PCT', 'PIE', 'IMPACT']].rename(columns={'PLAYER_NAME':'球員'}), hide_index=True)

            # 表格2: 官方傷病原文
            st.markdown("#### 🚑 2. NBA 官方通報傷病名單")
            ic1, ic2 = st.columns(2)
            with ic1:
                st.write(f"🏠 **{curr['h_cn']} 傷病名單**")
                if not curr['h_pkg']['inj_df'].empty: st.table(curr['h_pkg']['inj_df'][['球員', '狀態', '說明']])
                else: st.info("✅ 官方目前無傷病通報")
            with ic2:
                st.write(f"✈️ **{curr['a_cn']} 傷病名單**")
                if not curr['a_pkg']['inj_df'].empty: st.table(curr['a_pkg']['inj_df'][['球員', '狀態', '說明']])
                else: st.info("✅ 官方目前無傷病通報")
