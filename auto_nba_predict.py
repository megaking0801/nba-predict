import streamlit as st
import pandas as pd
import requests
import re
import unicodedata
from datetime import datetime, timedelta
import pytz
from bs4 import BeautifulSoup

# --- 1. 核心配置 ---
tw_tz = pytz.timezone('Asia/Taipei')
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}

TEAM_MAP = {
    'ATL': ['Atlanta Hawks', '老鷹'], 'BKN': ['Brooklyn Nets', '籃網'], 'BOS': ['Boston Celtics', '塞爾提克'],
    'CHA': ['Charlotte Hornets', '黃蜂'], 'CHI': ['Chicago Bulls', '公牛'], 'CLE': ['Cleveland Cavaliers', '騎士'],
    'DAL': ['Dallas Mavericks', '獨行俠'], 'DEN': ['Denver Nuggets', '金塊'], 'DET': ['Detroit Pistons', '活塞'],
    'GSW': ['Golden State Warriors', '勇士'], 'HOU': ['Houston Rockets', '火箭'], 'IND': ['Indiana Pacers', '溜馬'],
    'LAC': ['LA Clippers', '快艇', 'LAC'], 'LAL': ['Los Angeles Lakers', '湖人', 'LAL'], 'MEM': ['Memphis Grizzlies', '灰熊'],
    'MIA': ['Miami Heat', '熱火'], 'MIL': ['Milwaukee Bucks', '公鹿'], 'MIN': ['Minnesota Timberwolves', '灰狼'],
    'NOP': ['New Orleans Pelicans', '鵜鶘'], 'NYK': ['New York Knicks', '尼克'], 'OKC': ['Oklahoma City Thunder', '雷霆'],
    'ORL': ['Orlando Magic', '魔術'], 'PHI': ['Philadelphia 76ers', '76人'], 'PHX': ['Phoenix Suns', '太陽'],
    'POR': ['Portland Trail Blazers', '拓荒者'], 'SAC': ['Sacramento Kings', '國王'], 'SAS': ['San Antonio Spurs', '馬刺'],
    'TOR': ['Toronto Raptors', '暴龍'], 'UTA': ['Utah Jazz', '爵士'], 'WAS': ['Washington Wizards', '巫師']
}

# --- 2. 工具函數 ---
def normalize_name(name):
    if not isinstance(name, str): return ""
    name = ''.join(c for c in unicodedata.normalize('NFD', name) if unicodedata.category(c) != 'Mn')
    name = name.lower()
    name = re.sub(r'\b(jr\.?|sr\.?|ii|iii|iv)\b', '', name)
    name = re.sub(r'[.\']', '', name)
    return name.strip()

# --- 3. 數據抓取優化 ---

@st.cache_data(ttl=3600)
def load_global_stats():
    """從備用源抓取球員數據，採用索引抓取欄位以避開 'Tm' 報錯"""
    url = "https://www.basketball-reference.com/leagues/NBA_2026_per_game.html"
    try:
        # 使用多種解析器嘗試
        for flavor in ['lxml', 'html5lib']:
            try:
                dfs = pd.read_html(url, flavor=flavor)
                if dfs: break
            except: continue
        
        df = dfs[0]
        df = df[df['Player'] != 'Player'].copy()
        
        # 關鍵：不依賴 'Tm' 名稱，通常球隊在第 5 個欄位 (索引 4)
        df['TEAM_KEY'] = df.iloc[:, 4].astype(str)
        
        # 轉型
        cols_to_fix = {'PTS': 29, 'TRB': 23, 'AST': 24, 'STL': 25, 'BLK': 26, 'TOV': 27, 'FG%': 10}
        for col_name, idx in cols_to_fix.items():
            df[col_name] = pd.to_numeric(df.iloc[:, idx], errors='coerce').fillna(0)
            
        # 戰力公式 (包含板凳數據)
        df['IMPACT'] = df['PTS'] + df['TRB']*1.1 + df['AST']*1.5 + (df['STL']+df['BLK'])*2 - df['TOV']*2
        df['NORM'] = df['Player'].apply(normalize_name)
        return df
    except Exception as e:
        st.error(f"數據庫連線失敗: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=600)
def get_espn_live():
    """抓取賽程與傷病，加入多日期檢查邏輯"""
    injuries, games = [], []
    try:
        # 1. 抓傷病
        r_i = requests.get("https://www.espn.com/nba/injuries", headers=HEADERS, timeout=10)
        soup_i = BeautifulSoup(r_i.text, 'html.parser')
        for table in soup_i.select('.ResponsiveTable'):
            t_name = table.select_one('.Table__Title').get_text()
            t_abbr = next((a for a, i in TEAM_MAP.items() if any(n.lower() in t_name.lower() for n in i)), "UNK")
            for r in table.select('tbody tr'):
                tds = r.select('td')
                if len(tds) >= 3:
                    p_name = re.sub(r'(PG|SG|SF|PF|C|G|F)$', '', tds[0].get_text())
                    status = tds[2].get_text().strip()
                    is_out = any(w in status.lower() for w in ['out', 'doubtful', 'injured', 'surgery'])
                    injuries.append({'p': p_name, 'norm': normalize_name(p_name), 'team': t_abbr, 'status': status, 'out': is_out})
        
        # 2. 抓賽程 (嘗試抓取當前與未來)
        r_s = requests.get("https://www.espn.com/nba/schedule", headers=HEADERS, timeout=10)
        soup_s = BeautifulSoup(r_s.text, 'html.parser')
        for row in soup_s.select('.Table__TR'):
            anchors = row.select('.Table__Team a')
            if len(anchors) >= 2:
                a_n, h_n = anchors[0].get_text(), anchors[1].get_text()
                a_a = next((k for k, v in TEAM_MAP.items() if any(n.lower() in a_n.lower() for n in v)), "")
                h_a = next((k for k, v in TEAM_MAP.items() if any(n.lower() in h_n.lower() for n in v)), "")
                if a_a and h_a: games.append({'a': a_a, 'h': h_a})
    except: pass
    return pd.DataFrame(injuries), games

# --- 4. 介面與邏輯 ---
st.set_page_config(page_title="NBA Edge v14.3", layout="wide")
st.title("🏀 NBA 數據分析系統 (賽程修復版)")

df_db = load_global_stats()
df_inj, schedule = get_espn_live()

if df_db.empty:
    st.error("⚠️ 無法獲取基礎球員數據，請確認網路或重新整理。")
    st.stop()

if not schedule:
    st.warning("📅 抓不到賽程？這可能是時區導致的。你可以點擊下方手動輸入當日對戰：")
    manual_home = st.selectbox("手動選擇主隊", [""] + list(TEAM_MAP.keys()))
    manual_away = st.selectbox("手動選擇客隊", [""] + list(TEAM_MAP.keys()))
    if manual_home and manual_away:
        schedule = [{'h': manual_home, 'a': manual_away}]
    else:
        st.info("目前無自動抓取的比賽。")

if schedule:
    analysis = []
    for g in schedule:
        h_id, a_id = g['h'], g['a']
        
        def process(team_code):
            # 取得傷病名單
            outs = df_inj[(df_inj['team'] == team_code) & (df_inj['out'])]['norm'].tolist()
            # 關鍵：這裡抓取「所有球員」但排除「確定不上場的人」
            active = df_db[(df_db['TEAM_KEY'].str.contains(team_code, case=False, na=False)) & (~df_db['NORM'].isin(outs))]
            # 若沒抓到 (可能因縮寫)，擴大匹配
            if active.empty:
                active = df_db[(df_db['TEAM_KEY'].isin(TEAM_MAP[team_code])) & (~df_db['NORM'].isin(outs))]
            
            return {
                'pts': active['PTS'].sum(), 
                'impact': active['IMPACT'].mean(), 
                'df': active.sort_values('IMPACT', ascending=False),
                'inj': df_inj[df_inj['team'] == team_code]
            }

        h_data = process(h_id)
        a_data = process(a_id)
        
        # 修正分差權重：考慮到全員上場後的得分飽和
        raw_diff = (h_data['pts'] - a_data['pts']) * 0.1 + (h_data['impact'] - a_data['impact']) * 5 + 3.0
        prob = 1 / (1 + 10**(-abs(raw_diff)/12)) * 100
        
        analysis.append({
            'h_code': h_id, 'a_code': a_id, 'h_name': TEAM_MAP[h_id][1], 'a_name': TEAM_MAP[a_id][1],
            'diff': raw_diff, 'prob': prob, 'h_data': h_data, 'a_data': a_data
        })

    # --- Top 4 推薦 ---
    top_4 = sorted(analysis, key=lambda x: x['prob'], reverse=True)[:4]
    
    st.header(f"🔥 今日高勝率 Top {len(top_4)} 推薦")
    cols = st.columns(min(len(top_4), 2))
    
    for i, res in enumerate(top_4):
        with cols[i % 2]:
            with st.container(border=True):
                st.subheader(f"{res['a_name']} @ {res['h_name']}")
                
                g_in = st.columns(3)
                u_sp = g_in[0].number_input("讓分", 0.0, step=0.5, key=f"sp_{i}")
                u_oh = g_in[1].number_input("主賠", 1.90, step=0.01, key=f"oh_{i}")
                u_oa = g_in[2].number_input("客賠", 1.90, step=0.01, key=f"oa_{i}")
                
                final_margin = res['diff'] + u_sp
                rec_t = res['h_name'] if final_margin > 0 else res['a_name']
                win_p = res['prob'] if final_margin > 0 else (100 - res['prob'])
                ev = (win_p/100 * (u_oh if final_margin > 0 else u_oa)) - 1
                
                st.success(f"🎯 推薦：{rec_t} (勝率 {win_p:.1f}%)")
                st.write(f"📈 預測分差: {res['diff']:+.1f} | 期望值 (EV): {ev*100:+.1f}%")
                
                with st.expander("📝 查看預計上場名單"):
                    c1, c2 = st.columns(2)
                    c1.write(f"**{res['h_name']} 全員**")
                    c1.dataframe(res['h_data']['df'][['Player', 'PTS', 'IMPACT']].head(12), hide_index=True)
                    c2.write(f"**{res['a_name']} 全員**")
                    c2.dataframe(res['a_data']['df'][['Player', 'PTS', 'IMPACT']].head(12), hide_index=True)
