import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats
)
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests, re, unicodedata, time
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# --- 1. 核心配置與標頭 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')
us_east_tz = pytz.timezone('US/Eastern')

# 模擬真實瀏覽器，防止被 NBA 官網封鎖 IP
NBA_HEADERS = {
    'Host': 'stats.nba.com',
    'Connection': 'keep-alive',
    'Cache-Control': 'max-age=0',
    'Upgrade-Insecure-Requests': '1',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
}

# 判讀指南
def render_legend():
    st.sidebar.info("""
    **💡 數據判讀指南**
    - ⭐ **Edge > 3.0**: 價值投資
    - ⭐⭐ **Edge > 5.0**: 強力推薦
    - **EV > 10%**: 極佳獲利機會
    - **穩定度 > 70%**: 核心健康，適合串關
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

# --- 2. 工具函數 ---
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
    date_match = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d+)', text_str, re.IGNORECASE)
    if date_match:
        months = {'Jan':1, 'Feb':2, 'Mar':3, 'Apr':4, 'May':5, 'Jun':6, 'Jul':7, 'Aug':8, 'Sep':9, 'Oct':10, 'Nov':11, 'Dec':12}
        return f"❌ 缺陣 (預計 {months.get(date_match.group(1)[:3].title(), 0)}/{date_match.group(2)} 歸隊)"
    trans = {r'\bOut\b': '❌ 缺陣', r'\bDay-To-Day\b': '📋 每日觀察', r'\bGTD\b': '📋 賽前決定', r'\bQuestionable\b': '🤔 出戰成疑', r'\bDoubtful\b': '😰 極大機率缺陣', r'\bProbable\b': '✅ 可能出戰'}
    for eng, chi in trans.items(): text_str = re.sub(eng, chi, text_str, flags=re.IGNORECASE)
    return text_str

def fetch_safe_df(endpoint, **kwargs):
    """強化版 API 抓取函數，加入重試與標頭"""
    for _ in range(2): # 失敗會重試一次
        try:
            r = endpoint(headers=NBA_HEADERS, timeout=30, **kwargs).get_dict()
            res = r['resultSets'][0]
            return pd.DataFrame(res['rowSet'], columns=res['headers'])
        except:
            time.sleep(2)
    return pd.DataFrame()

def evaluate_stability(inj_df, top_players_names):
    gtd_list = ['📋', '🤔', 'GTD', 'Day-To-Day', 'Questionable']
    core_gtd = 0
    if not inj_df.empty:
        for _, row in inj_df.iterrows():
            if any(x in row['狀態'] for x in gtd_list) and row['NORMALIZED_NAME'] in top_players_names:
                core_gtd += 1
    if core_gtd >= 2: return "🔴 極高風險 (核心變數大)", 20
    if core_gtd == 1: return "🟡 中度風險 (建議觀望)", 60
    return "🟢 穩定性高 (適合隔夜)", 95

def get_edge_stars(edge, ev, stability_score):
    if edge > 5.5 and ev > 0.1: b = 5
    elif edge > 3.5 and ev > 0.05: b = 4
    elif edge > 1.5 and ev > 0: b = 3
    else: b = 2
    return "⭐" * (min(b, 2) if stability_score < 50 else b)

def format_stats_df(df):
    if df is None or df.empty: return pd.DataFrame()
    rename_map = {'PLAYER_NAME':'球員', 'PTS':'得分', 'FG_PCT':'命中%', 'FG3_PCT':'三分%', 'REB':'籃板', 'AST':'助攻', 'PIE':'貢獻值'}
    d_df = df[[c for c in rename_map.keys() if c in df.columns]].copy()
    d_df.rename(columns=rename_map, inplace=True)
    for c in ['命中%', '三分%']: 
        if c in d_df.columns: d_df[c] = (d_df[c] * 100).round(1).astype(str) + '%'
    return d_df

# --- 3. 數據引擎 ---
@st.cache_data(ttl=600)
def get_espn_injuries_v31():
    url = "https://www.espn.com/nba/injuries"
    all_inj = []
    try:
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for table in soup.select('.ResponsiveTable'):
            t_title = table.select_one('.Table__Title').get_text(strip=True)
            t_abbr = next((a for a, info in TEAM_MAP.items() if any(n.lower() in t_title.lower() for n in info)), "UNK")
            for r in table.select('tbody tr'):
                cols = r.select('td')
                if len(cols) >= 3:
                    p_name = re.sub(r'(PG|SG|SF|PF|C|G|F)$', '', cols[0].get_text(strip=True))
                    raw_status, raw_comm = cols[2].get_text(strip=True), cols[3].get_text(strip=True) if len(cols)>3 else ""
                    is_out = any(w in (raw_status + raw_comm).lower() for w in ['out', 'doubtful', 'injured']) or bool(re.search(r'[A-Z][a-z]{2}\s\d+', raw_status))
                    all_inj.append({'球員': p_name, 'NORMALIZED_NAME': normalize_name(p_name), '狀態': translate_status(raw_status), '說明': raw_comm, '球隊': t_abbr, 'IS_OUT': is_out})
    except: pass
    return pd.DataFrame(all_inj)

@st.cache_data(ttl=3600)
def load_nba_stats_v31():
    S = '2025-26'
    p_base = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    p_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    if p_base.empty or p_adv.empty: return pd.DataFrame()
    p_full = pd.merge(p_base, p_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID', how='left')
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    p_full['NORMALIZED_NAME'] = p_full['PLAYER_NAME'].apply(normalize_name)
    return p_full

# --- 4. 主程式 UI ---
st.set_page_config(page_title="NBA Edge 專家 v13.31", layout="wide")
render_legend()
st.title("🏀 NBA 數據預測 v13.31 (API 強化版)")

ps_db = load_nba_stats_v31()
injury_df = get_espn_injuries_v31()
all_season_games = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable='2025-26')

if ps_db.empty:
    st.error("❌ 無法取得球員數據。這通常是 NBA 官網暫時封鎖了連線。請嘗試重新整理或稍候再試。")
    st.stop()

# 尋找最近比賽
nba_today = datetime.now(us_east_tz)
sb = pd.DataFrame()
target_date = nba_today

for i in range(14):
    curr_d = nba_today + timedelta(days=i)
    temp_sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=curr_d.strftime('%m/%d/%Y'))
    if not temp_sb.empty and not temp_sb[temp_sb['HOME_TEAM_ID'].isin(VALID_TEAM_IDS)].empty:
        sb = temp_sb[temp_sb['HOME_TEAM_ID'].isin(VALID_TEAM_IDS)]
        target_date = curr_d
        break

if sb.empty:
    st.warning("📅 近期無正規賽事。")
else:
    id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
    st.markdown(f"### 📅 分析日期：{target_date.strftime('%Y-%m-%d')} (美東)")
    
    # 預算所有比賽過盤率
    analysis_list = []
    yesterday_str = (target_date - timedelta(days=1)).strftime('%Y-%m-%d')

    for idx, row in sb.iterrows():
        h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
        h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
        if not h_abbr or not a_abbr: continue

        def get_team_pkg(tid, abbr):
            t_inj = injury_df[injury_df['球隊'] == abbr] if not injury_df.empty else pd.DataFrame()
            out_names = t_inj[t_inj['IS_OUT']]['NORMALIZED_NAME'].tolist()
            # 抓取「所有」可用球員
            active_ps = ps_db[(ps_db['TEAM_ID'] == tid) & (~ps_db['NORMALIZED_NAME'].isin(out_names))].sort_values('IMPACT', ascending=False)
            is_b2b = not all_season_games[(all_season_games['TEAM_ID'] == tid) & (all_season_games['GAME_DATE'] == yesterday_str)].empty if not all_season_games.empty else False
            status, score = evaluate_stability(t_inj, active_ps.head(8)['NORMALIZED_NAME'].tolist())
            return {'pts': active_ps['PTS'].sum(), 'pie': active_ps['PIE'].mean(), 'df': active_ps, 'inj': t_inj, 'b2b': is_b2b, 'status': status, 'score': score}

        h_pkg, a_pkg = get_team_pkg(h_id, h_abbr), get_team_pkg(a_id, a_abbr)
        
        # 預測核心模型
        diff = (h_pkg['pts'] - a_pkg['pts']) * 0.12 + (h_pkg['pie'] - a_pkg['pie']) * 45 + 2.5
        if h_pkg['b2b']: diff -= 1.5
        if a_pkg['b2b']: diff += 1.5
        
        # 基礎機率 (不計讓分)
        base_prob = 1 / (1 + 10**(-abs(diff)/8)) * 100
        analysis_list.append({
            'idx': idx, 'h_cn': TEAM_NAME_CH.get(h_abbr, h_abbr), 'a_cn': TEAM_NAME_CH.get(a_abbr, a_abbr),
            'diff': diff, 'base_prob': base_prob, 'h_pkg': h_pkg, 'a_pkg': a_pkg
        })

    # 挑選 Top 4
    analysis_list = sorted(analysis_list, key=lambda x: x['base_prob'], reverse=True)[:4]
    
    # 渲染介面
    grid = st.columns(3)
    rankings = []
    for i, game in enumerate(analysis_list):
        with grid[i % 3]:
            with st.container(border=True):
                st.markdown(f"#### 🏀 {game['a_cn']} @ {game['h_cn']}")
                c_sp, c_h, c_a = st.columns([2, 1, 1])
                u_sp = c_sp.number_input(f"讓分", 0.0, step=0.5, key=f"sp_{i}")
                u_oh = c_h.number_input(f"主賠", 1.01, 5.0, 1.90, 0.01, key=f"oh_{i}")
                u_oa = c_a.number_input(f"客賠", 1.01, 5.0, 1.90, 0.01, key=f"oa_{i}")
                
                final_diff = game['diff'] + u_sp
                prob = (1 / (1 + 10**(-final_diff/8)) * 100) if final_diff >= 0 else (1 - 1 / (1 + 10**(-final_diff/8))) * 100
                rec = game['h_cn'] if final_diff >= 0 else game['a_cn']
                odds = u_oh if final_diff >= 0 else u_oa
                ev = (prob/100 * odds) - 1
                stab = (game['h_pkg']['score'] + game['a_pkg']['score']) / 2
                
                if stab > 70: st.success(f"🔥 推薦：{rec}")
                else: st.warning(f"⚠️ 推薦：{rec} (風險)")
                
                st.write(f"**勝率：{prob:.1f}% | EV：{ev*100:+.1f}%**")
                st.write(f"📊 {get_edge_stars(abs(final_diff), ev, stab)} (Edge: {abs(final_diff):.1f})")
                rankings.append({'rec': rec, 'stab': stab, 'ev': ev})

    st.sidebar.header("🎯 隔夜串關建議")
    safe_picks = [r['rec'] for r in rankings if r['stab'] > 70 and r['ev'] > 0]
    if len(safe_picks) >= 2: st.sidebar.success(f"推薦組合：\n{safe_picks[0]} + {safe_picks[1]}")
    else: st.sidebar.info("今日無高穩定度串關建議")

    st.divider()
    st.markdown("### 🔍 深度對戰比較 (全員名單)")
    sel = st.selectbox("挑選比賽", [f"{g['a_cn']} @ {g['h_cn']}" for g in analysis_list])
    curr = next(g for g in analysis_list if f"{g['a_cn']} @ {g['h_cn']}" == sel)
    
    c1, c2 = st.columns(2)
    for col, p, side in zip([c1, c2], [curr['h_pkg'], curr['a_pkg']], ["主", "客"]):
        with col:
            st.subheader(f"{side}隊：{p['status']}")
            st.caption("🚑 傷病")
            st.dataframe(p['inj'][['球員', '狀態', '說明']] if not p['inj'].empty else pd.DataFrame(columns=['✅ 健康']), hide_index=True)
            st.caption("💪 會上場的所有球員數據")
            st.dataframe(format_stats_df(p['df']), hide_index=True)
