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

# --- 2. 數據抓取引擎 ---
@st.cache_data(ttl=600)
def get_espn_injuries_v31():
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
def load_nba_stats_v31():
    # 獲取最新賽季數據 (2025-26)
    p_full = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season='2025-26', per_mode_detailed='PerGame')
    if p_full.empty: return pd.DataFrame()
    # 全員分析公式：得分 + 籃板權重 + 助攻權重 + (抄截+火鍋)*2 - 失誤*2
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    p_full['NORM'] = p_full['PLAYER_NAME'].apply(normalize_name)
    return p_full

# --- 3. UI 主架構 ---
st.set_page_config(page_title="NBA Edge 專家 v14.5", layout="wide")
st.title("🏀 NBA 數據預測 (數據完整版)")

ps_db = load_nba_stats_v31()
injury_df = get_espn_injuries_v31()

# 獲取今日賽程
nba_today = datetime.now(us_east_tz)
date_str = nba_today.strftime('%m/%d/%Y')
sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=date_str)
if sb.empty or sb[sb['HOME_TEAM_ID'].isin(VALID_TEAM_IDS)].empty:
    # 嘗試抓取明天
    date_str = (nba_today + timedelta(days=1)).strftime('%m/%d/%Y')
    sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=date_str)

id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}

if sb.empty:
    st.info("📅 今日暫無比賽數據。")
else:
    all_game_results = []
    sb_filtered = sb[sb['HOME_TEAM_ID'].isin(VALID_TEAM_IDS)]
    
    for _, row in sb_filtered.iterrows():
        h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
        h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
        
        def get_team_data(tid, abbr):
            # 取得傷病名單
            t_inj = injury_df[injury_df['球隊'] == abbr] if not injury_df.empty else pd.DataFrame()
            out_names = t_inj[t_inj['IS_OUT']]['NORM'].tolist()
            # 獲取該隊所有人，並排除 Out 的球員
            active = ps_db[(ps_db['TEAM_ID'] == tid) & (~ps_db['NORM'].isin(out_names))].sort_values('IMPACT', ascending=False)
            return {'pts': active['PTS'].sum(), 'impact': active['IMPACT'].mean(), 'df': active, 'inj': t_inj}

        h_pkg = get_team_data(h_id, h_abbr)
        a_pkg = get_team_data(a_id, a_abbr)
        
        # 預測分差公式
        raw_diff = (h_pkg['pts'] - a_pkg['pts']) * 0.12 + (h_pkg['impact'] - a_pkg['impact']) * 5 + 2.5
        prob = 1 / (1 + 10**(-abs(raw_diff)/8)) * 100
        
        all_game_results.append({
            'label': f"{TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}",
            'h_cn': TEAM_NAME_CH.get(h_abbr), 'a_cn': TEAM_NAME_CH.get(a_abbr),
            'diff': raw_diff, 'prob': prob, 'h_pkg': h_pkg, 'a_pkg': a_pkg
        })

    # --- Top 4 推薦排序 ---
    top_4 = sorted(all_game_results, key=lambda x: x['prob'], reverse=True)[:4]

    st.header(f"🔥 今日 Top {len(top_4)} 預盤推薦")
    
    for idx, g in enumerate(top_4):
        with st.expander(f"📊 {g['label']} - 預估勝率 {g['prob']:.1f}%", expanded=True):
            col_ui, col_h, col_a = st.columns([1, 1, 1])
            
            with col_ui:
                st.subheader("🎯 投資建議")
                u_sp = st.number_input("讓分 (主+客-)", 0.0, step=0.5, key=f"sp_{idx}")
                u_oh = st.number_input("主賠", 1.01, 5.0, 1.90, key=f"oh_{idx}")
                u_oa = st.number_input("客賠", 1.01, 5.0, 1.90, key=f"oa_{idx}")
                
                f_diff = g['diff'] + u_sp
                rec = g['h_cn'] if f_diff > 0 else g['a_cn']
                st.info(f"建議：**{rec}**\n預測分差：{g['diff']:+.1f}")
            
            with col_h:
                st.subheader(f"🏠 {g['h_cn']} 核心戰力")
                st.dataframe(g['h_pkg']['df'][['PLAYER_NAME', 'PTS', 'IMPACT']].head(8), hide_index=True)
                if not g['h_pkg']['inj'].empty:
                    st.caption("🚑 傷病名單")
                    st.dataframe(g['h_pkg']['inj'][['球員', '狀態']], hide_index=True)

            with col_a:
                st.subheader(f"✈️ {g['a_cn']} 核心戰力")
                st.dataframe(g['a_pkg']['df'][['PLAYER_NAME', 'PTS', 'IMPACT']].head(8), hide_index=True)
                if not g['a_pkg']['inj'].empty:
                    st.caption("🚑 傷病名單")
                    st.dataframe(g['a_pkg']['inj'][['球員', '狀態']], hide_index=True)
