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

# --- 1. 核心配置 ---
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

# 傷病狀態翻譯對照表
STATUS_MAP = {
    'Out': '❌ 缺陣',
    'Day-To-Day': '📋 每日觀察',
    'GTD': '📋 賽前決定',
    'Questionable': '🤔 出戰成疑',
    'Doubtful': '😰 極大機率缺陣',
    'Health and Safety Protocols': '🛡️ 健康安全協議'
}

def translate_status(stat_text):
    for eng, chi in STATUS_MAP.items():
        if eng in stat_text: return chi
    return stat_text

st.set_page_config(page_title="NBA 數據專家 v12.8", layout="wide")

# --- 2. ESPN 傷病解析引擎 (v12.8 精準定位+中文翻譯版) ---
@st.cache_data(ttl=600)
def get_espn_injuries_v2():
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    all_inj = []
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 尋找所有球隊區塊
        # ESPN 的結構通常是 div.Table__Title (含隊名) 後面接 div.Table__Scroller (含表格)
        containers = soup.select('.ResponsiveTable')
        
        for container in containers:
            title_div = container.select_one('.Table__Title')
            if not title_div: continue
            
            full_team_name = title_div.get_text(strip=True)
            t_abbr = "UNKNOWN"
            for abbr, chi in TEAM_NAME_CH.items():
                if chi in full_team_name or abbr in full_team_name.upper():
                    t_abbr = abbr
                    break
            
            # 精準定位表格行
            rows = container.select('tbody tr')
            for r in rows:
                cols = r.select('td')
                if len(cols) >= 3:
                    # ESPN 表格通常是：0:球員(含位置), 1:狀態, 2:說明
                    raw_player = cols[0].get_text(strip=True)
                    # 移除球員名稱後面的位置標記 (例如 "Ja MorantPG")
                    player_name = raw_player.replace('PG','').replace('SG','').replace('SF','').replace('PF','').replace('C','')
                    
                    status_eng = cols[1].get_text(strip=True)
                    all_inj.append({
                        '球員': player_name,
                        '狀態': translate_status(status_eng),
                        '說明': cols[2].get_text(strip=True),
                        '球隊': t_abbr,
                        'RAW_STATUS': status_eng # 供過濾使用
                    })
    except Exception as e:
        st.error(f"傷病數據抓取失敗: {e}")
    
    df = pd.DataFrame(all_inj)
    for col in ['球員', '狀態', '說明', '球隊', 'RAW_STATUS']:
        if col not in df.columns: df[col] = ""
    return df

@st.cache_data(ttl=3600)
def load_all_nba_stats():
    S = '2025-26'
    p_base = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    p_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    
    if p_base.empty: return pd.DataFrame(), {}, "N/A"
    
    p_full = pd.merge(p_base, p_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID', how='left')
    # IMPACT = 得分 + 籃板*1.1 + 助攻*1.5 + (抄截+火鍋)*2 - 失誤*2
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    
    gf = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    l10 = {}
    if not gf.empty:
        l10 = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(10, min_periods=3).mean()).groupby(gf['TEAM_ID']).last().to_dict()
    
    return p_full, l10, datetime.now(tw_tz).strftime("%H:%M")

def fetch_safe_df(endpoint, **kwargs):
    try:
        r = endpoint(**kwargs).get_dict()
        res = r['resultSets'][0] if 'resultSets' in r else r['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

# --- 數據同步 ---
ps_db, l10_db, update_time = load_all_nba_stats()
injury_df = get_espn_injuries_v2()

# --- 3. UI 顯示邏輯 ---
st.title("🏀 NBA 數據專家 v12.8 (傷病翻譯+精準修復)")
st.sidebar.write(f"📊 最後同步: {update_time}")

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
            
            def build_team_package(tid, abbr):
                t_inj = injury_df[injury_df['球隊'] == abbr]
                # 過濾不上的球員 (依據原始英文狀態判斷)
                out_names = t_inj[t_inj['RAW_STATUS'].str.contains('Out|Doubtful|Day-To-Day|GTD', case=False, na=False)]['球員'].tolist()
                
                all_ps = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
                # 排除傷病名單球員，選取最強 8 人
                active_core = all_ps[~all_ps['PLAYER_NAME'].apply(lambda x: any(name in x for name in out_names))].head(8)
                
                return {'pts': active_core['PTS'].sum(), 'ts': active_core['TS_PCT'].mean(), 
                        'pie': active_core['PIE'].mean(), 'df': active_core, 'inj_df': t_inj}

            h_res = build_team_package(h_id, h_abbr)
            a_res = build_team_package(a_id, a_abbr)

            final_margin = (h_res['pts']-a_res['pts'])*0.12 + (h_res['ts']-a_res['ts'])*15 + (h_res['pie']-a_res['pie'])*45 + (l10_db.get(h_id,0)-l10_db.get(a_id,0))*0.4 + 2.5
            prob_h = 1 / (1 + 10**(-final_margin/15)) * 100
            
            h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
            g_key = f"v128_{dates[i].strftime('%Y%m%d')}_{a_abbr}_{h_abbr}"

            with cols[idx % 3]:
                with st.container(border=True):
                    st.markdown(f"### [客] {a_cn} vs [主] {h_cn}")
                    st.metric(f"{h_cn} 勝率", f"{prob_h:.1f}%")
                    st.metric(f"{a_cn} 勝率", f"{100-prob_h:.1f}%")
                    
                    show_odds = st.toggle("顯示盤口分析", key=f"tog_{g_key}")
                    if show_odds:
                        c1, c2 = st.columns(2)
                        oh = c1.number_input(f"[主隊] 賠率", value=1.85, key=f"h_{g_key}")
                        oa = c2.number_input(f"[客隊] 賠率", value=1.85, key=f"a_{g_key}")
                        edge = (prob_h - (1/oh*100)) if prob_h > 50 else ((100-prob_h) - (1/oa*100))
                        st.info(f"💡 價值優勢: {edge:+.1f}%")

                    results.append({'label': f"[客] {a_cn} vs [主] {h_cn}", 'h_res': h_res, 'a_res': a_res, 'h_cn': h_cn, 'a_cn': a_cn})

        if results:
            st.divider()
            sel = st.selectbox("🔍 選擇對戰分析查看傷病", [x['label'] for x in results], key=f"sel_{i}")
            curr = next(x for x in results if x['label'] == sel)

            st.markdown("#### 📊 1. 預計上場核心數據 (排除缺陣球員)")
            c1, c2 = st.columns(2)
            c1.write(f"**[主隊] {curr['h_cn']} 可用球員**")
            c1.dataframe(curr['h_res']['df'][['PLAYER_NAME', 'PTS', 'TS_PCT', 'PIE', 'IMPACT']].rename(columns={'PLAYER_NAME':'球員'}), hide_index=True)
            c2.write(f"**[客隊] {curr['a_cn']} 可用球員**")
            c2.dataframe(curr['a_res']['df'][['PLAYER_NAME', 'PTS', 'TS_PCT', 'PIE', 'IMPACT']].rename(columns={'PLAYER_NAME':'球員'}), hide_index=True)

            st.markdown("#### 🚑 2. ESPN 即時傷病詳情 (已翻譯)")
            ic1, ic2 = st.columns(2)
            with ic1:
                st.write(f"**{curr['h_cn']} 傷情**")
                if not curr['h_res']['inj_df'].empty: st.table(curr['h_res']['inj_df'][['球員', '狀態', '說明']])
                else: st.success("✅ 目前無傷病紀錄")
            with ic2:
                st.write(f"**{curr['a_cn']} 傷情**")
                if not curr['a_res']['inj_df'].empty: st.table(curr['a_res']['inj_df'][['球員', '狀態', '說明']])
                else: st.success("✅ 目前無傷病紀錄")
