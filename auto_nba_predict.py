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

st.set_page_config(page_title="NBA 數據專家 v13.11", layout="wide")

# --- 2. 數據抓取 (含全文字掃描過濾) ---
@st.cache_data(ttl=600)
def get_espn_injuries_v8():
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
                    # 解決截圖中 Zubac 顯示日期但說明有 Out 的衝突
                    full_check = (col2 + " " + col3).lower()
                    is_out = any(word in full_check for word in ['out', 'doubtful', 'injured', '❌', '缺陣'])
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
injury_df = get_espn_injuries_v8()

# --- 3. UI 顯示 ---
st.title("🏀 NBA 數據專家 v13.11 (盤口深度分析版)")

nba_now = datetime.now(us_east_tz)
tab1, tab2 = st.tabs(["今日比賽預測", "數據分析中心"])

with tab1:
    sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=nba_now.strftime('%m/%d/%Y'))
    if sb.empty: st.info("📅 今日暫無比賽"); st.stop()
    
    id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
    cols = st.columns(2)
    
    for idx, row in sb.iterrows():
        h_abbr, a_abbr = id_map.get(row['HOME_TEAM_ID']), id_map.get(row['VISITOR_TEAM_ID'])
        if not h_abbr or not a_abbr: continue
        
        def get_team_stats(tid, abbr):
            t_inj = injury_df[injury_df['球隊'] == abbr]
            out_names = t_inj[t_inj['IS_OUT']]['NORMALIZED_NAME'].tolist()
            all_ps = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
            active = all_ps[~all_ps['NORMALIZED_NAME'].isin(out_names)].head(8)
            return {'pts': active['PTS'].sum(), 'pie': active['PIE'].mean(), 'df': active, 'inj': t_inj}

        h_data, a_data = get_team_stats(row['HOME_TEAM_ID'], h_abbr), get_team_stats(row['VISITOR_TEAM_ID'], a_abbr)
        h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
        
        # 模型預估分差
        raw_diff = (h_data['pts'] - a_data['pts']) * 0.15 + (h_data['pie'] - a_data['pie']) * 50 + 2.5
        
        with cols[idx % 2]:
            with st.container(border=True):
                st.subheader(f"{a_cn} @ {h_cn}")
                
                # --- 運彩輸入區 ---
                st.markdown("##### 📥 運彩盤口輸入")
                c1, c2, c3 = st.columns([2, 1, 1])
                user_spread = c1.number_input(f"主隊 ({h_cn}) 讓分", value=0.0, step=0.5, key=f"sp_{idx}", help="讓分輸入負數(如-5.5)，受讓輸入正數(如+3.5)")
                user_odd_h = c2.number_input("主賠率", value=1.75, key=f"oh_{idx}")
                user_odd_a = c3.number_input("客賠率", value=1.75, key=f"oa_{idx}")
                
                # --- 分析模型 ---
                # 莊家隱含機率
                prob_h_odds = (1/user_odd_h) / (1/user_odd_h + 1/user_odd_a) * 100
                # 數據預估過盤率 (模型分差 vs 實際盤口)
                edge = raw_diff + user_spread # 如果模型預估贏10分，盤口主讓5.5 (-5.5)，edge就是 4.5
                cover_prob = 1 / (1 + 10**(-edge/10)) * 100

                # --- 顯示結果 ---
                st.divider()
                res_c1, res_c2 = st.columns(2)
                res_c1.metric("模型預估分差", f"{h_cn} {raw_diff:+.1f}")
                
                if edge > 2.0:
                    res_c2.success(f"✅ 推薦: {h_cn} 過盤")
                    st.toast(f"🔥 {h_cn} 盤口有價值!")
                elif edge < -2.0:
                    res_c2.error(f"✅ 推薦: {a_cn} 過盤")
                else:
                    res_c2.info("⚖️ 盤口精準")

                st.progress(cover_prob/100, text=f"模型預估 {h_cn} 過盤率: {cover_prob:.1f}%")
                
                with st.expander("查看核心傷兵過濾"):
                    if not h_data['inj'][h_data['inj']['IS_OUT']].empty:
                        st.write(f"🚫 {h_cn} 已排除: {', '.join(h_data['inj'][h_data['inj']['IS_OUT']]['球員'])}")
                    st.dataframe(h_data['df'][['PLAYER_NAME', 'PTS', 'PIE']], hide_index=True)
