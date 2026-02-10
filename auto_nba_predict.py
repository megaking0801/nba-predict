import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats
)
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests, re, unicodedata
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# --- 1. 核心配置 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')
us_east_tz = pytz.timezone('US/Eastern')

TEAM_MAP = {
    'ATL': ['Atlanta Hawks', '老鷹'], 'BKN': ['Brooklyn Nets', '籃網'], 'BOS': ['Boston Celtics', '塞爾提克'],
    'CHA': ['Charlotte Hornets', '黃蜂'], 'CHI': ['Chicago Bulls', '公牛'], 'CLE': ['Cleveland Cavaliers', '騎士'],
    'DAL': ['Dallas Mavericks', '獨行俠'], 'DEN': ['Denver Nuggets', '金塊'], 'DET': ['Detroit Pistons', '活塞'],
    'GSW': ['Golden State Warriors', '勇士'], 'HOU': ['Houston Rockets', '火箭'], 'IND': ['Indiana Pacers', '溜馬'],
    'LAC': ['LA Clippers', '快艇'], 'LAL': ['Los Angeles Lakers', '湖人'], 'MEM': ['Memphis Grizzlies', '灰熊'],
    'MIA': ['Miami Heat', '熱火'], 'MIL': ['Milwaukee Bucks', '公鹿'], 'MIN': ['Minnesota Timberwolves', '灰狼'],
    'NOP': ['New Orleans Pelicans', '鵜鶘'], 'NYK': ['New York Knicks', '尼克'], 'OKC': ['Oklahoma City Thunder', '雷霆'],
    'ORL': ['Orlando Magic', '魔術'], 'PHI': ['Philadelphia 76ers', '76人'], 'PHX': ['Phoenix Suns', '太陽'],
    'POR': ['Portland Trail Blazers', '拓荒者'], 'SAC': ['Sacramento Kings', '國王'], 'SAS': ['San Antonio Spurs', '馬刺'],
    'TOR': ['Toronto Raptors', '暴龍'], 'UTA': ['Utah Jazz', '爵士'], 'WAS': ['Washington Wizards', '巫師']
}

TEAM_NAME_CH = {k: v[1] for k, v in TEAM_MAP.items()}

# --- 工具函數 ---
def normalize_name(name):
    if not isinstance(name, str): return ""
    name = ''.join(c for c in unicodedata.normalize('NFD', name) if unicodedata.category(c) != 'Mn')
    name = name.lower()
    name = re.sub(r'\b(jr\.?|sr\.?|ii|iii|iv)\b', '', name)
    name = re.sub(r'[.\']', '', name)
    nicknames = {'nic ': 'nicolas ', 'cam ': 'cameron ', 'chris ': 'christopher ', 'pjwashington': 'pj washington'}
    for nick, full in nicknames.items():
        if name.startswith(nick): name = name.replace(nick, full)
    return name.strip()

def translate_text(text):
    if not text or pd.isna(text): return ""
    res = str(text)
    trans = {
        r'\bOut\b': '❌ 缺陣', r'\bDay-To-Day\b': '📋 每日觀察', r'\bGTD\b': '📋 賽前決定',
        r'\bQuestionable\b': '🤔 出戰成疑', r'\bDoubtful\b': '😰 極大機率缺陣', r'\bProbable\b': '✅ 可能出戰',
        r'\bG\b': '後衛', r'\bF\b': '前鋒', r'\bC\b': '中鋒'
    }
    for eng, chi in trans.items():
        res = re.sub(eng, chi, res, flags=re.IGNORECASE)
    return res

st.set_page_config(page_title="NBA 數據專家 v13.12", layout="wide")

# --- 2. 數據抓取引擎 ---
@st.cache_data(ttl=600)
def get_espn_injuries_v9():
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0'}
    all_inj = []
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for table in soup.select('.ResponsiveTable'):
            t_name = table.select_one('.Table__Title').get_text(strip=True)
            t_abbr = next((a for a, info in TEAM_MAP.items() if any(n.lower() in t_name.lower() for n in info)), "UNK")
            for r in table.select('tbody tr'):
                cols = r.select('td')
                if len(cols) >= 3:
                    p_name = re.sub(r'(PG|SG|SF|PF|C|G|F)$', '', cols[0].get_text(strip=True))
                    col2, col3 = cols[2].get_text(strip=True), cols[3].get_text(strip=True) if len(cols)>3 else ""
                    # 解決欄位錯位判定，掃描全文字
                    full_check = (col2 + " " + col3).lower()
                    is_out = any(word in full_check for word in ['out', 'doubtful', 'injured', '缺陣', '❌'])
                    all_inj.append({
                        '球員': p_name, 'NORMALIZED_NAME': normalize_name(p_name),
                        '位置': translate_text(cols[1].get_text(strip=True)),
                        '狀態': translate_text(col2), '說明': col3, '球隊': t_abbr, 'IS_OUT': is_out
                    })
    except: pass
    return pd.DataFrame(all_inj)

@st.cache_data(ttl=3600)
def load_nba_stats():
    S = '2025-26'
    p_base = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    p_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    p_full = pd.merge(p_base, p_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID', how='left')
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    p_full['NORMALIZED_NAME'] = p_full['PLAYER_NAME'].apply(normalize_name)
    return p_full

def fetch_safe_df(endpoint, **kwargs):
    try:
        r = endpoint(**kwargs).get_dict()
        res = r['resultSets'][0]
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

# --- 數據準備 ---
ps_db = load_nba_stats()
injury_df = get_espn_injuries_v9()

# --- 3. UI 顯示 ---
st.title("🏀 NBA 數據專家 v13.12")
st.sidebar.info("📌 v13.12: 數據與賠率深度融合 + 傷病詳細比較區下移")

nba_now = datetime.now(us_east_tz)
sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=nba_now.strftime('%m/%d/%Y'))

if sb.empty:
    st.info("📅 今日暫無比賽排程")
else:
    id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
    all_game_data = [] # 用於存儲每場比賽，最後在底部顯示
    
    st.markdown("### 🏟️ 今日賽程預測與盤口分析")
    grid = st.columns(3)
    
    for idx, row in sb.iterrows():
        h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
        h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
        if not h_abbr or not a_abbr: continue
        
        # 建立數據包
        def get_pkg(tid, abbr):
            t_inj = injury_df[injury_df['球隊'] == abbr]
            out_list = t_inj[t_inj['IS_OUT']]['NORMALIZED_NAME'].tolist()
            all_ps = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
            active = all_ps[~all_ps['NORMALIZED_NAME'].isin(out_list)].head(8)
            return {'pts': active['PTS'].sum(), 'pie': active['PIE'].mean(), 'df': active, 'inj': t_inj, 'ex': all_ps[all_ps['NORMALIZED_NAME'].isin(out_list)]['PLAYER_NAME'].tolist()}

        h_pkg, a_pkg = get_pkg(h_id, h_abbr), get_pkg(a_id, a_abbr)
        h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
        
        # 1. 基礎數據勝率 (v13.10 邏輯)
        raw_diff = (h_pkg['pts'] - a_pkg['pts']) * 0.12 + (h_pkg['pie'] - a_pkg['pie']) * 45 + 2.5
        model_prob_h = 1 / (1 + 10**(-raw_diff/15)) * 100
        
        g_key = f"v1312_{idx}"
        
        with grid[idx % 3]:
            with st.container(border=True):
                st.markdown(f"#### {a_cn} @ {h_cn}")
                
                # 盤口輸入區
                c_sp, c_h, c_a = st.columns([2, 1, 1])
                u_spread = c_sp.number_input("主隊讓分", value=0.0, step=0.5, key=f"sp_{g_key}", help="主讓請輸負數(-5.5)，主受讓請輸正數(+3.5)")
                u_oh = c_h.number_input("主賠", value=1.75, key=f"oh_{g_key}")
                u_oa = c_a.number_input("客賠", value=1.75, key=f"oa_{g_key}")
                
                # 2. 賠率融合 (v13.10 邏輯)
                imp_prob_h = (1/u_oh) / (1/u_oh + 1/u_oa) * 100
                final_prob_h = (model_prob_h * 0.6) + (imp_prob_h * 0.4)
                
                # 3. 過盤價值分析
                edge = raw_diff + u_spread # 分差優勢
                
                st.divider()
                st.metric(f"{h_cn} 綜合勝率", f"{final_prob_h:.1f}%", delta=f"{final_prob_h-model_prob_h:.1f}% (賠率修正)")
                st.caption(f"📊 模型預估分差: {h_cn} {raw_diff:+.1f}")
                
                if edge > 2.0:
                    st.success(f"🔥 價值推薦: {h_cn} 過盤 (優勢 {abs(edge):.1f}分)")
                elif edge < -2.0:
                    st.error(f"🔥 價值推薦: {a_cn} 過盤 (優勢 {abs(edge):.1f}分)")
                else:
                    st.info("⚖️ 盤口符合實力差距")
        
        # 存入列表供底部顯示
        all_game_data.append({
            'label': f"{a_cn} (客) vs {h_cn} (主)",
            'h_cn': h_cn, 'a_cn': a_cn,
            'h_pkg': h_pkg, 'a_pkg': a_pkg
        })

    # --- 4. 底部額外數據比較區 ---
    st.divider()
    st.markdown("### 🔍 對戰詳細數據比較 (傷病名單與核心名單)")
    
    if all_game_data:
        # 使用 Selectbox 選擇想看的詳細對戰
        sel_game = st.selectbox("選擇對戰組合以查看詳細對比", [g['label'] for g in all_game_data])
        curr = next(g for g in all_game_data if g['label'] == sel_game)
        
        # 顯示傷病對比
        st.markdown(f"#### 🚑 {sel_game} - 傷病報告")
        i_col1, i_col2 = st.columns(2)
        with i_col1:
            st.write(f"**[主] {curr['h_cn']}**")
            if not curr['h_pkg']['inj'].empty:
                st.dataframe(curr['h_pkg']['inj'][['球員', '位置', '狀態', 'IS_OUT']], hide_index=True, use_container_width=True)
            else: st.success("✅ 全員健康")
        with i_col2:
            st.write(f"**[客] {curr['a_cn']}**")
            if not curr['a_pkg']['inj'].empty:
                st.dataframe(curr['a_pkg']['inj'][['球員', '位置', '狀態', 'IS_OUT']], hide_index=True, use_container_width=True)
            else: st.success("✅ 全員健康")
            
        st.divider()
        
        # 顯示核心 8 人對比
        st.markdown(f"#### 🛡️ {sel_game} - 核心 8 人戰力 (已自動過濾傷兵)")
        p_col1, p_col2 = st.columns(2)
        with p_col1:
            st.write(f"**{curr['h_cn']} 核心**")
            if curr['h_pkg']['ex']: st.error(f"🚫 已排除: {', '.join(curr['h_pkg']['ex'])}")
            st.dataframe(curr['h_pkg']['df'][['PLAYER_NAME', 'PTS', 'REB', 'AST', 'PIE']], hide_index=True, use_container_width=True)
        with p_col2:
            st.write(f"**{curr['a_cn']} 核心**")
            if curr['a_pkg']['ex']: st.error(f"🚫 已排除: {', '.join(curr['a_pkg']['ex'])}")
            st.dataframe(curr['a_pkg']['df'][['PLAYER_NAME', 'PTS', 'REB', 'AST', 'PIE']], hide_index=True, use_container_width=True)
