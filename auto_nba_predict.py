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
VALID_TEAM_IDS = [t['id'] for t in teams.get_teams()]

def normalize_name(name):
    if not isinstance(name, str): return ""
    name = ''.join(c for c in unicodedata.normalize('NFD', name) if unicodedata.category(c) != 'Mn')
    name = name.lower()
    name = re.sub(r'\b(jr\.?|sr\.?|ii|iii|iv)\b', '', name)
    name = re.sub(r'[.\']', '', name)
    return name.strip()

def translate_status(text):
    if not text or pd.isna(text): return ""
    text_str = str(text).strip()
    trans = {r'\bOut\b': '❌ 缺陣', r'\bDay-To-Day\b': '📋 觀察', r'\bGTD\b': '📋 賽前決定', r'\bQuestionable\b': '🤔 出戰成疑'}
    for eng, chi in trans.items():
        text_str = re.sub(eng, chi, text_str, flags=re.IGNORECASE)
    return text_str

def fetch_safe_df(endpoint, **kwargs):
    try:
        r = endpoint(**kwargs).get_dict()
        res = r['resultSets'][0]
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

# --- 2. 數據抓取 ---
@st.cache_data(ttl=600)
def get_espn_injuries():
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
                    st_val = cols[2].get_text(strip=True)
                    all_inj.append({
                        '球員': p_name, 'NORM': normalize_name(p_name),
                        '狀態': translate_status(st_val), '球隊': t_abbr, 
                        'IS_OUT': any(w in st_val.lower() for w in ['out', 'doubtful', 'injured'])
                    })
    except: pass
    return pd.DataFrame(all_inj)

@st.cache_data(ttl=3600)
def load_nba_stats():
    p_full = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season='2025-26', per_mode_detailed='PerGame')
    if p_full.empty: return pd.DataFrame()
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    p_full['NORM'] = p_full['PLAYER_NAME'].apply(normalize_name)
    return p_full

# --- 3. UI 邏輯 ---
st.set_page_config(page_title="NBA Edge v14.6", layout="wide")
st.title("🏀 NBA 數據預測系統")

ps_db = load_nba_stats()
injury_df = get_espn_injuries()

# 獲取賽程 (優先今日，若無則明日)
nba_today = datetime.now(us_east_tz)
date_str = nba_today.strftime('%m/%d/%Y')
sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=date_str)
if sb.empty or sb[sb['HOME_TEAM_ID'].isin(VALID_TEAM_IDS)].empty:
    date_str = (nba_today + timedelta(days=1)).strftime('%m/%d/%Y')
    sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=date_str)

id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}

if sb.empty:
    st.warning("📅 暫無賽程數據。")
else:
    all_game_results = []
    sb_filtered = sb[sb['HOME_TEAM_ID'].isin(VALID_TEAM_IDS)]
    
    for _, row in sb_filtered.iterrows():
        h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
        h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
        
        def get_team_data(tid, abbr):
            t_inj = injury_df[injury_df['球隊'] == abbr] if not injury_df.empty else pd.DataFrame()
            out_names = t_inj[t_inj['IS_OUT']]['NORM'].tolist()
            # 全員分析：抓取所有人並排除缺陣者
            active = ps_db[(ps_db['TEAM_ID'] == tid) & (~ps_db['NORM'].isin(out_names))].sort_values('IMPACT', ascending=False)
            return {'pts': active['PTS'].sum(), 'impact': active['IMPACT'].mean(), 'df': active, 'inj': t_inj}

        h_pkg = get_team_data(h_id, h_abbr)
        a_pkg = get_team_data(a_id, a_abbr)
        h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
        
        raw_diff = (h_pkg['pts'] - a_pkg['pts']) * 0.12 + (h_pkg['impact'] - a_pkg['impact']) * 5 + 2.5
        prob = 1 / (1 + 10**(-abs(raw_diff)/8)) * 100
        
        all_game_results.append({
            'label': f"{a_cn} @ {h_cn}", 'h_cn': h_cn, 'a_cn': a_cn,
            'diff': raw_diff, 'prob': prob, 'h_pkg': h_pkg, 'a_pkg': a_pkg
        })

    # --- 第一部分：今日所有場次勝率 ---
    st.subheader("📋 今日所有場次預測")
    summary_df = []
    for g in all_game_results:
        winner = g['h_cn'] if g['diff'] > 0 else g['a_cn']
        summary_df.append({"對戰組合": g['label'], "預計勝隊": winner, "原始勝率": f"{g['prob']:.1f}%", "預估分差": f"{g['diff']:+.1f}"})
    st.table(summary_df)

    # --- 第二部分：最強推薦 Top 4 ---
    st.divider()
    top_4 = sorted(all_game_results, key=lambda x: x['prob'], reverse=True)[:4]
    st.subheader("🔥 系統最強推薦 (Top 4)")
    cols = st.columns(len(top_4))
    for i, g in enumerate(top_4):
        with cols[i]:
            with st.container(border=True):
                st.markdown(f"**{g['label']}**")
                u_sp = st.number_input("讓分", 0.0, step=0.5, key=f"sp_{i}")
                f_diff = g['diff'] + u_sp
                rec = g['h_cn'] if f_diff > 0 else g['a_cn']
                st.success(f"推薦：{rec}")
                st.caption(f"原始勝率：{g['prob']:.1f}%")

    # --- 第三部分：深度數據選取區 ---
    st.divider()
    st.subheader("🔍 深度數據分析 (選取場次查看球員與傷病)")
    selected_label = st.selectbox("請選擇欲查看的場次", [g['label'] for g in all_game_results])
    
    if selected_label:
        curr = next(g for g in all_game_results if g['label'] == selected_label)
        c_h, c_a = st.columns(2)
        
        with c_h:
            st.markdown(f"### 🏠 {curr['h_cn']}")
            st.write("**預計出賽人員戰力 (全員)**")
            st.dataframe(curr['h_pkg']['df'][['PLAYER_NAME', 'PTS', 'REB', 'AST', 'IMPACT']], hide_index=True, use_container_width=True)
            st.write("**🚑 傷病名單**")
            st.dataframe(curr['h_pkg']['inj'][['球員', '狀態']] if not curr['h_pkg']['inj'].empty else "✅ 全員健康", hide_index=True, use_container_width=True)

        with c_a:
            st.markdown(f"### ✈️ {curr['a_cn']}")
            st.write("**預計出賽人員戰力 (全員)**")
            st.dataframe(curr['a_pkg']['df'][['PLAYER_NAME', 'PTS', 'REB', 'AST', 'IMPACT']], hide_index=True, use_container_width=True)
            st.write("**🚑 傷病名單**")
            st.dataframe(curr['a_pkg']['inj'][['球員', '狀態']] if not curr['a_pkg']['inj'].empty else "✅ 全員健康", hide_index=True, use_container_width=True)
