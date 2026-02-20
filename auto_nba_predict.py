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

def render_legend():
    st.sidebar.info("""
    **💡 數據判讀指南**
    **Edge (讓分優勢)**
    - ⭐ **> 3.0**: 價值投資
    - ⭐⭐ **> 5.0**: 強力推薦
    
    **EV (期望值)**
    - 📈 **> 5%**: 值得進場
    """)

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

# --- 工具函數 ---
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
    trans = {r'\bOut\b': '❌ 缺陣', r'\bDay-To-Day\b': '📋 每日觀察', r'\bGTD\b': '📋 賽前決定', r'\bQuestionable\b': '🤔 出戰成疑'}
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
                    raw_status = cols[2].get_text(strip=True)
                    is_out = any(word in (raw_status).lower() for word in ['out', 'doubtful', 'injured'])
                    all_inj.append({
                        '球員': p_name, 'NORMALIZED_NAME': normalize_name(p_name),
                        '狀態': translate_status(raw_status), '球隊': t_abbr, 'IS_OUT': is_out
                    })
    except: pass
    return pd.DataFrame(all_inj)

@st.cache_data(ttl=3600)
def load_nba_stats_v31():
    S = '2025-26'
    p_full = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    if p_full.empty: return pd.DataFrame()
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    p_full['NORMALIZED_NAME'] = p_full['PLAYER_NAME'].apply(normalize_name)
    return p_full

# --- 3. UI 主架構 ---
st.set_page_config(page_title="NBA Edge 專家 v14.0", layout="wide")
render_legend()
st.title("🏀 NBA 數據預測 (修復版)")

ps_db = load_nba_stats_v31()
injury_df = get_espn_injuries_v31()

# 獲取賽程
nba_today = datetime.now(us_east_tz)
sb = pd.DataFrame()
for i in range(7):
    date_str = (nba_today + timedelta(days=i)).strftime('%m/%d/%Y')
    temp_sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=date_str)
    if not temp_sb.empty:
        sb = temp_sb[temp_sb['HOME_TEAM_ID'].isin(VALID_TEAM_IDS)]
        if not sb.empty: break

id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}

if sb.empty:
    st.error("⚠️ 抓不到賽程。")
else:
    all_game_results = []
    for idx, row in sb.iterrows():
        h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
        h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
        
        def get_team_pkg(tid, abbr):
            t_inj = injury_df[injury_df['球隊'] == abbr] if not injury_df.empty else pd.DataFrame()
            out_names = t_inj[t_inj['IS_OUT']]['NORMALIZED_NAME'].tolist()
            # 獲取所有球員數據，但排除確定缺陣者
            active_players = ps_db[(ps_db['TEAM_ID'] == tid) & (~ps_db['NORMALIZED_NAME'].isin(out_names))].sort_values('IMPACT', ascending=False)
            return {'pts': active_players['PTS'].sum(), 'impact': active_players['IMPACT'].mean(), 'df': active_players, 'inj': t_inj}

        h_pkg, a_pkg = get_team_pkg(h_id, h_abbr), get_team_pkg(a_id, a_abbr)
        h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
        
        # 預測模型
        raw_diff = (h_pkg['pts'] - a_pkg['pts']) * 0.12 + (h_pkg['impact'] - a_pkg['impact']) * 5 + 2.5
        all_game_results.append({
            'label': f"{a_cn} @ {h_cn}", 'h_cn': h_cn, 'a_cn': a_cn,
            'raw_diff': raw_diff, 'h_pkg': h_pkg, 'a_pkg': a_pkg
        })

    # --- 關鍵：挑選過盤率最高的四場 (依據預測勝率排序) ---
    def calc_prob(diff): return 1 / (1 + 10**(-diff/8)) * 100
    
    # 計算每場的預設勝率 (假設無讓分情況下) 並排序
    for g in all_game_results:
        g['base_prob'] = calc_prob(abs(g['raw_diff']))
    
    top_4 = sorted(all_game_results, key=lambda x: x['base_prob'], reverse=True)[:4]

    st.markdown(f"### 🔥 今日最強推薦 Top {len(top_4)}")
    grid = st.columns(min(len(top_4), 2))
    
    for i, g in enumerate(top_4):
        with grid[i % 2]:
            with st.container(border=True):
                st.subheader(g['label'])
                c1, c2, c3 = st.columns(3)
                u_sp = c1.number_input("讓分 (主+客-)", 0.0, step=0.5, key=f"sp_{i}")
                u_oh = c2.number_input("主賠", 1.01, 5.0, 1.90, key=f"h_{i}")
                u_oa = c3.number_input("客賠", 1.01, 5.0, 1.90, key=f"a_{i}")
                
                final_diff = g['raw_diff'] + u_sp
                win_prob = calc_prob(abs(final_diff))
                rec_team = g['h_cn'] if final_diff > 0 else g['a_cn']
                sel_odds = u_oh if final_diff > 0 else u_oa
                ev = (win_prob/100 * sel_odds) - 1
                
                st.success(f"🎯 推薦：{rec_team} (勝率 {win_prob:.1f}%)")
                st.write(f"期望值 EV: {ev*100:+.1f}% | 預測分差: {g['raw_diff']:+.1f}")
                
                with st.expander("查看上場球員數據"):
                    st.write(f"**{g['h_cn']} 可用球員**")
                    st.dataframe(g['h_pkg']['df'][['PLAYER_NAME', 'PTS', 'IMPACT']].head(10), hide_index=True)
                    st.write(f"**{g['a_cn']} 可用球員**")
                    st.dataframe(g['a_pkg']['df'][['PLAYER_NAME', 'PTS', 'IMPACT']].head(10), hide_index=True)
