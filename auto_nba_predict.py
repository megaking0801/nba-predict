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

# --- 1. 配置與中文化 ---
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

st.set_page_config(page_title="NBA 專家 v12.5", layout="wide")

# --- 2. 核心數據抓取：ESPN 傷病源 (最穩定) ---
@st.cache_data(ttl=600)
def get_espn_injuries():
    """解析 ESPN 傷病頁面，這是目前抓取 SGA 等傷兵最穩定的方式"""
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0'}
    all_inj = []
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        # 由於環境限制，這裡模擬 BeautifulSoup 解析邏輯的穩定產出
        # 實際部署時請確保 bs4 已安裝
        soup = BeautifulSoup(resp.text, 'html.parser')
        sections = soup.find_all('div', class_='Table__Title')
        
        for section in sections:
            team_name = section.text.strip()
            # 取得隊名縮寫
            t_abbr = "UNKNOWN"
            for abbr, chi in TEAM_NAME_CH.items():
                if chi in team_name or abbr in team_name.upper():
                    t_abbr = abbr; break
            
            table = section.find_next('table')
            if table:
                rows = table.find_all('tr')[1:] # 跳過表頭
                for r in rows:
                    cols = r.find_all('td')
                    if len(cols) >= 3:
                        all_inj.append({
                            '球員': cols[0].text.strip(),
                            '狀態': cols[1].text.strip(),
                            '說明': cols[2].text.strip(),
                            '球隊': t_abbr
                        })
    except: pass
    
    df = pd.DataFrame(all_inj)
    if df.empty or '球隊' not in df.columns:
        return pd.DataFrame(columns=['球員', '狀態', '說明', '球隊'])
    return df

@st.cache_data(ttl=3600)
def load_master_data():
    S = '2025-26'
    p_base = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    p_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    if p_base.empty: return pd.DataFrame(), {}, "N/A"
    p_full = pd.merge(p_base, p_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID', how='left')
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    gf = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    l10 = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(10, min_periods=3).mean()).groupby(gf['TEAM_ID']).last().to_dict()
    return p_full, l10, datetime.now(tw_tz).strftime("%H:%M")

def fetch_safe_df(endpoint, **kwargs):
    try:
        r = endpoint(**kwargs).get_dict()
        res = r['resultSets'][0] if 'resultSets' in r else r['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

# --- 3. 數據初始化 ---
ps_db, l10_db, update_time = load_master_data()
injury_df = get_espn_injuries()

# --- 4. 主 UI 介面 ---
st.title("🏀 NBA 數據專家 v12.5 (ESPN 源 + 盤口回歸)")
st.sidebar.write(f"📊 數據同步: {update_time}")

nba_now = datetime.now(us_east_tz)
dates = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 暫無比賽排程"); continue

        id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        results = []
        cols = st.columns(3)

        for idx, row in sb.iterrows():
            h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
            h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
            if not h_abbr or not a_abbr: continue
            
            def get_team_analysis(tid, abbr):
                t_inj = injury_df[injury_df['球隊'].str.contains(abbr, na=False, case=False)]
                # 排除 Out 或狀態不明的傷兵
                out_names = t_inj[t_inj['狀態'].str.contains('Out|Doubtful|Day-To-Day', case=False, na=False)]['球員'].tolist()
                
                all_ps = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
                # 會上場的核心 (排除傷兵)
                active_core = all_ps[~all_ps['PLAYER_NAME'].apply(lambda x: any(name in x for name in out_names))].head(8)
                
                return {'pts': active_core['PTS'].sum(), 'ts': active_core['TS_PCT'].mean(), 
                        'pie': active_core['PIE'].mean(), 'df': active_core, 'inj_df': t_inj}

            h_res = get_team_analysis(h_id, h_abbr)
            a_res = get_team_analysis(a_id, a_abbr)

            # 勝率模型
            final_margin = (h_res['pts']-a_res['pts'])*0.12 + (h_res['ts']-a_res['ts'])*15 + (h_res['pie']-a_res['pie'])*45 + (l10_db.get(h_id,0)-l10_db.get(a_id,0))*0.4 + 2.5
            prob_h = 1 / (1 + 10**(-final_margin/15)) * 100
            
            h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
            g_key = f"v125_{dates[i].strftime('%Y%m%d')}_{a_abbr}_{h_abbr}"

            with cols[idx % 3]:
                with st.container(border=True):
                    st.markdown(f"### [客隊] {a_cn} vs [主隊] {h_cn}")
                    st.metric(f"{h_cn} [主隊] 勝率", f"{prob_h:.1f}%")
                    st.metric(f"{a_cn} [客隊] 勝率", f"{100-prob_h:.1f}%")
                    
                    # 盤口開關加回
                    show_odds = st.toggle("顯示盤口計算", key=f"tog_{g_key}")
                    if show_odds:
                        oh = st.number_input(f"{h_cn} [主隊] 賠率", value=1.85, key=f"h_{g_key}")
                        oa = st.number_input(f"{a_cn} [客隊] 賠率", value=1.85, key=f"a_{g_key}")
                        edge = (prob_h - (1/oh*100)) if prob_h > 50 else ((100-prob_h) - (1/oa*100))
                        st.info(f"💡 價值優勢: {edge:+.1f}%")

                    results.append({'label': f"[客隊] {a_cn} vs [主隊] {h_cn}", 'h_res': h_res, 'a_res': a_res, 'h_cn': h_cn, 'a_cn': a_cn})

        # --- 詳細數據大表 ---
        if results:
            st.divider()
            sel = st.selectbox("🔍 選擇對戰查看分析", [x['label'] for x in results], key=f"sel_{i}")
            curr = next(x for x in results if x['label'] == sel)

            st.markdown("#### 📊 1. 預計上場核心球員數據")
            c1, c2 = st.columns(2)
            c1.write(f"**[主隊] {curr['h_cn']} 可用球員**")
            c1.dataframe(curr['h_res']['df'][['PLAYER_NAME', 'PTS', 'TS_PCT', 'PIE', 'IMPACT']].rename(columns={'PLAYER_NAME':'球員'}), hide_index=True)
            c2.write(f"**[客隊] {curr['a_cn']} 可用球員**")
            c2.dataframe(curr['a_res']['df'][['PLAYER_NAME', 'PTS', 'TS_PCT', 'PIE', 'IMPACT']].rename(columns={'PLAYER_NAME':'球員'}), hide_index=True)

            st.markdown("#### 🚑 2. ESPN 即時傷病詳情")
            ic1, ic2 = st.columns(2)
            with ic1:
                st.write(f"**[主隊] {curr['h_cn']} 傷情**")
                if not curr['h_res']['inj_df'].empty: st.table(curr['h_res']['inj_df'][['球員', '狀態', '說明']])
                else: st.success("✅ 目前無傷病紀錄")
            with ic2:
                st.write(f"**[客隊] {curr['a_cn']} 傷情**")
                if not curr['a_res']['inj_df'].empty: st.table(curr['a_res']['inj_df'][['球員', '狀態', '說明']])
                else: st.success("✅ 目前無傷病紀錄")
