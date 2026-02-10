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

st.set_page_config(page_title="NBA 專家 v12.1", layout="wide")

# --- 2. Rotowire 專用傷病引擎 ---
@st.cache_data(ttl=600)  # 每 10 分鐘更新一次，捕捉臨場變動
def get_rotowire_injuries():
    """全量抓取 Rotowire 傷病名單，包含詳細狀態與傷情描述"""
    url = "https://www.rotowire.com/basketball/injury-report.php"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        rows = soup.find_all('tr', class_='is-nba')
        data = []
        for r in rows:
            # 抓取球員、球隊、狀態、說明
            name = r.find('a', class_='injuries__name').text.strip() if r.find('a', class_='injuries__name') else "未知"
            team = r.find('div', class_='injuries__team').text.strip() if r.find('div', class_='injuries__team') else ""
            status = r.find('td', class_='injuries__status').text.strip() if r.find('td', class_='injuries__status') else ""
            desc = r.find('td', class_='injuries__comment').text.strip() if r.find('td', class_='injuries__comment') else ""
            data.append({'球員': name, '狀態': status, '球隊': team, '說明': desc})
        
        df = pd.DataFrame(data)
        if not df.empty:
            return df
    except Exception as e:
        st.error(f"Rotowire 抓取失敗: {e}")
    return pd.DataFrame(columns=['球員', '狀態', '球隊', '說明'])

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
injury_df = get_rotowire_injuries()

# --- 3. UI 顯示邏輯 ---
st.title("🏀 NBA 數據專家 v12.1 (Rotowire 即時傷病強化版)")
st.sidebar.markdown(f"**傷病來源: Rotowire (即時)**\n\n**數據更新: {update_time}**")

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
            
            def get_team_bundle(tid, abbr):
                # 模糊匹配球隊縮寫 (Rotowire 可能使用不同格式)
                t_inj = injury_df[injury_df['球隊'].str.contains(abbr, na=False, case=False)]
                
                # 確定「不打」的名單：Out, Doubtful, 或有受傷描述且狀態不明者
                out_names = t_inj[t_inj['狀態'].str.contains('Out|Doubtful|Inact', case=False, na=False)]['球員'].tolist()
                
                # 會上場的核心球員數據 (前 8 名)
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

            # 勝率模型計算
            pts_val = (h_m['pts_sum'] - a_m['pts_sum']) * 0.12
            eff_val = (h_m['ts_avg'] - a_m['ts_avg']) * 15 + (h_m['pie_avg'] - a_m['pie_avg']) * 45
            trend_val = (l10_db.get(h_id, 0) - l10_db.get(a_id, 0)) * 0.4
            final_margin = pts_val + eff_val + trend_val + 2.5
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
            sel = st.selectbox("🔍 選擇對戰查看分析", [x['label'] for x in results], key=f"sel_{i}")
            curr = next(x for x in results if x['label'] == sel)

            st.markdown("#### 📊 1. 預計上場核心數據 (參與勝率分析)")
            pc1, pc2 = st.columns(2)
            pc1.write(f"**[主隊] {curr['h_cn']} 核心成員**")
            pc1.dataframe(curr['h_m']['df'][['PLAYER_NAME', 'PTS', 'TS_PCT', 'PIE', 'IMPACT']].rename(columns={'PLAYER_NAME':'球員'}), hide_index=True)
            pc2.write(f"**[客隊] {curr['a_cn']} 核心成員**")
            pc2.dataframe(curr['a_m']['df'][['PLAYER_NAME', 'PTS', 'TS_PCT', 'PIE', 'IMPACT']].rename(columns={'PLAYER_NAME':'球員'}), hide_index=True)

            st.markdown("#### 🚑 2. Rotowire 完整傷病通報 (包含 GTD)")
            ic1, ic2 = st.columns(2)
            with ic1:
                st.write(f"**[主隊] {curr['h_cn']} 傷病名單**")
                if not curr['h_m']['inj_df'].empty: st.table(curr['h_m']['inj_df'][['球員', '狀態', '說明']])
                else: st.success("✅ 目前無傷病紀錄")
            with ic2:
                st.write(f"**[客隊] {curr['a_cn']} 傷病名單**")
                if not curr['a_m']['inj_df'].empty: st.table(curr['a_m']['inj_df'][['球員', '狀態', '說明']])
                else: st.success("✅ 目前無傷病紀錄")
