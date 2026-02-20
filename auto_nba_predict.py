import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats
)
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests, re, unicodedata, time
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
    - ⭐ **1.0 ~ 3.0**: 輕微優勢
    - ⭐⭐ **3.0 ~ 5.0**: 價值投資
    - ⭐⭐⭐ **> 5.0**: 強烈推薦
    
    **EV (期望值)**
    - 📈 **> 3%**: 值得進場
    - 🔥 **> 8%**: 極佳機會
    
    **穩定度 (Stability)**
    - 🟢 **> 70%**: 適合隔夜
    - 🔴 **< 40%**: 建議臨場再投
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
    return re.sub(r'[.\']', '', name).strip()

def translate_status(text):
    if not text or pd.isna(text): return ""
    text_str = str(text).strip()
    date_match = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d+)', text_str, re.IGNORECASE)
    if date_match:
        months = {'Jan':1, 'Feb':2, 'Mar':3, 'Apr':4, 'May':5, 'Jun':6, 'Jul':7, 'Aug':8, 'Sep':9, 'Oct':10, 'Nov':11, 'Dec':12}
        m_num = months.get(date_match.group(1)[:3].title(), 0)
        return f"❌ 缺陣 (預計 {m_num}/{date_match.group(2)} 歸隊)"
    trans = { r'\bOut\b': '❌ 缺陣', r'\bDay-To-Day\b': '📋 每日觀察', r'\bGTD\b': '📋 賽前決定', r'\bQuestionable\b': '🤔 出戰成疑', r'\bDoubtful\b': '😰 極大機率缺陣', r'\bProbable\b': '✅ 可能出戰' }
    for eng, chi in trans.items(): text_str = re.sub(eng, chi, text_str, flags=re.IGNORECASE)
    return text_str

# 新增重試機制與錯誤捕捉
def fetch_safe_df(endpoint_class, retries=2, **kwargs):
    for attempt in range(retries):
        try:
            ep = endpoint_class(**kwargs)
            r = ep.get_dict()
            res = r['resultSets'][0]
            df = pd.DataFrame(res['rowSet'], columns=res['headers'])
            if not df.empty: return df
        except Exception as e:
            time.sleep(1) # 暫停 1 秒避免被伺服器阻擋
    return pd.DataFrame()

def evaluate_stability(inj_df, top_players_names):
    gtd_list = ['📋', '🤔', 'GTD', 'Day-To-Day', 'Questionable']
    core_gtd = sum(1 for _, row in inj_df.iterrows() if any(x in row['狀態'] for x in gtd_list) and row['NORMALIZED_NAME'] in top_players_names)
    if core_gtd >= 2: return "🔴 極高風險 (核心變數大)", 20
    if core_gtd == 1: return "🟡 中度風險 (建議觀望)", 60
    return "🟢 穩定性高 (適合隔夜)", 95

def get_edge_stars(edge, ev, stability_score):
    if edge > 5.0 and ev > 0.08: return "⭐⭐⭐⭐⭐"
    if edge > 3.5 and ev > 0.04: return "⭐⭐⭐⭐"
    if edge > 2.0 and ev > 0.02: return "⭐⭐⭐"
    return "⭐⭐"

def format_stats_df(df):
    if df.empty: return df
    d_df = df[['PLAYER_NAME', 'PTS', 'FG_PCT', 'FG3_PCT', 'REB', 'AST', 'PIE']].copy()
    d_df.rename(columns={'PLAYER_NAME':'球員', 'PTS':'得分', 'FG_PCT':'命中%', 'FG3_PCT':'三分%', 'REB':'籃板', 'AST':'助攻', 'PIE':'貢獻值'}, inplace=True)
    for c in ['命中%', '三分%']: d_df[c] = (d_df[c] * 100).round(1).astype(str) + '%'
    for c in ['得分', '籃板', '助攻', '貢獻值']: d_df[c] = d_df[c].round(1)
    return d_df

# --- 2. 數據抓取引擎 ---
@st.cache_data(ttl=600)
def get_espn_injuries_v33():
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
                    raw_s, raw_c = cols[2].get_text(strip=True), cols[3].get_text(strip=True) if len(cols)>3 else ""
                    is_out = any(w in (raw_s+raw_c).lower() for w in ['out','doubtful','injured','❌']) or bool(re.search(r'[A-Z][a-z]{2}\s\d+', raw_s))
                    all_inj.append({'球員': p_name, 'NORMALIZED_NAME': normalize_name(p_name), '位置': cols[1].get_text(strip=True), '狀態': translate_status(raw_s), '說明': raw_c, '球隊': t_abbr, 'IS_OUT': is_out})
    except: pass
    return pd.DataFrame(all_inj)

@st.cache_data(ttl=3600)
def load_nba_stats_v33():
    S = '2025-26'
    p_base = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    p_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    if p_base.empty: return pd.DataFrame()
    p_full = pd.merge(p_base, p_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID', how='left')
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    p_full['NORMALIZED_NAME'] = p_full['PLAYER_NAME'].apply(normalize_name)
    return p_full

# --- 3. UI 主架構 ---
st.set_page_config(page_title="NBA Edge 專家 v13.33", layout="wide")
render_legend()
st.title("🏀 NBA 數據預測 v13.33 (防護升級版)")

with st.spinner("正在連線 NBA 官方資料庫，請稍候... (若卡住超過 30 秒請重新整理)"):
    ps_db = load_nba_stats_v33()
    injury_df = get_espn_injuries_v33()
    # 增加過濾條件，減少 API 負載
    all_season_games = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable='2025-26', league_id_nullable='00')

if ps_db.empty:
    st.error("🚨 嚴重錯誤：無法載入 NBA 球員數據。這通常是因為 NBA API 暫時阻擋了連線，請等待 3-5 分鐘後重新整理頁面。")
    st.stop()

# 日期搜尋
nba_today = datetime.now(us_east_tz)
sb = pd.DataFrame()
found_regular = False
target_date = nba_today

with st.spinner("正在搜尋近期正規賽程..."):
    for i in range(14):
        search_date = nba_today + timedelta(days=i)
        # 使用 YYYY-MM-DD 格式，NBA API 相容性更好
        temp_sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=search_date.strftime('%Y-%m-%d'))
        if not temp_sb.empty and 'HOME_TEAM_ID' in temp_sb.columns:
            reg_games = temp_sb[temp_sb['HOME_TEAM_ID'].isin(VALID_TEAM_IDS)]
            if not reg_games.empty:
                sb, found_regular, target_date = reg_games, True, search_date
                break

if not found_regular:
    st.warning("⚠️ 未來 14 天內無正規賽，或是 API 未回傳賽程表。")
    st.stop()

st.markdown(f"### 📅 分析日期：{target_date.strftime('%Y-%m-%d')} (美東時間)")
all_game_data, rankings = [], []
grid = st.columns(3)
id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}

for idx, row in sb.iterrows():
    h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
    h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
    if not h_abbr or not a_abbr: continue
    
    def get_pkg(tid, abbr, date):
        is_b2b = not all_season_games[(all_season_games['TEAM_ID'] == tid) & (all_season_games['GAME_DATE'] == (date - timedelta(days=1)).strftime('%Y-%m-%d'))].empty if not all_season_games.empty else False
        t_inj = injury_df[injury_df['球隊'] == abbr] if not injury_df.empty else pd.DataFrame(columns=['IS_OUT', 'NORMALIZED_NAME', '狀態', '說明', '球員'])
        out_names = t_inj[t_inj['IS_OUT']]['NORMALIZED_NAME'].tolist() if 'IS_OUT' in t_inj.columns else []
        all_ps = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
        top_8 = all_ps[~all_ps['NORMALIZED_NAME'].isin(out_names)].head(8)
        status_text, s_score = evaluate_stability(t_inj, top_8['NORMALIZED_NAME'].tolist())
        return {'pts': top_8['PTS'].sum(), 'pie': top_8['PIE'].mean(), 'df': top_8, 'inj': t_inj, 'b2b': is_b2b, 'status': status_text, 'score': s_score}

    h_pkg, a_pkg = get_pkg(h_id, h_abbr, target_date), get_pkg(a_id, a_abbr, target_date)
    h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
    
    raw_diff = (h_pkg['pts'] - a_pkg['pts']) * 0.11 + (h_pkg['pie'] - a_pkg['pie']) * 42 + 2.0
    if h_pkg['b2b']: raw_diff -= 1.0
    if a_pkg['b2b']: raw_diff += 1.0
    
    g_key = f"v33_{idx}"
    with grid[idx % 3]:
        with st.container(border=True):
            st.markdown(f"#### 🏀 {a_cn} (客) @ {h_cn} (主)")
            c_sp, c_h, c_a = st.columns([2, 1, 1])
            u_spread = c_sp.number_input(f"讓分", value=0.0, step=0.5, key=f"sp_{g_key}")
            u_oh, u_oa = c_h.number_input(f"主賠", 1.01, 5.0, 1.90, 0.01, key=f"oh_{g_key}"), c_a.number_input(f"客賠", 1.01, 5.0, 1.90, 0.01, key=f"oa_{g_key}")
            
            edge_val = raw_diff + u_spread 
            cover_prob = min(max(1 / (1 + 10**(-edge_val/12)) * 100, 15.0), 85.0)
            
            if edge_val >= 0: disp_prob, rec_team, sel_odds = cover_prob, h_cn, u_oh
            else: disp_prob, rec_team, sel_odds = (100-cover_prob), a_cn, u_oa
            
            ev = (disp_prob/100 * sel_odds) - 1
            final_edge = abs(edge_val)
            avg_stab = (h_pkg['score'] + a_pkg['score']) / 2
            
            st.divider()
            st.success(f"🔥 推薦：{rec_team}") if avg_stab > 70 else st.warning(f"⚠️ 推薦：{rec_team}")
            c1, c2 = st.columns(2)
            c1.metric("勝率", f"{disp_prob:.1f}%")
            c2.metric("EV", f"{ev*100:+.1f}%")
            st.write(f"📊 評級：{get_edge_stars(final_edge, ev, avg_stab)} | Edge：{final_edge:.1f}")

            rankings.append({'matchup': f"{a_cn}@{h_cn}", 'rec_team': rec_team, 'prob': disp_prob, 'edge': final_edge, 'ev': ev, 'stability': avg_stab})
    all_game_data.append({'label': f"{a_cn} (客) @ {h_cn} (主)", 'h_pkg': h_pkg, 'a_pkg': a_pkg, 'h_cn': h_cn, 'a_cn': a_cn})

# Top 4 推薦區 (遵循：不足四場全推，超過四場推勝率前四，預設賠率自動運作)
st.markdown("### 🏆 過盤率最高推薦 (Top 4)")
rankings.sort(key=lambda x: x['prob'], reverse=True)
n_show = min(len(rankings), 4)
if n_show > 0:
    cols = st.columns(n_show)
    for i in range(n_show):
        r = rankings[i]
        cols[i].info(f"**TOP {i+1}**\n\n{r['matchup']}\n**{r['rec_team']}**\n🎯 {r['prob']:.1f}%")
else:
    st.write("目前無賽事可推薦。")

st.divider()
st.markdown("### 🔍 詳細數據 (含前 8 人數據表)")
if all_game_data:
    curr = next((g for g in all_game_data if g['label'] == st.selectbox("選擇對戰組合", [g['label'] for g in all_game_data])), None)
    if curr:
        h, a = st.columns(2)
        for col, p, side in zip([h, a], [curr['h_pkg'], curr['a_pkg']], ["🏠 [主]", "✈️ [客]"]):
            with col:
                st.header(f"{side} {curr['h_cn' if '主' in side else 'a_cn']}")
                st.caption(f"穩定度: {p['status']}")
                st.markdown("#### 🚑 傷病名單"); st.dataframe(p['inj'][['球員', '狀態', '說明']] if not p['inj'].empty else pd.DataFrame(columns=['✅ 健康']), hide_index=True)
                st.markdown("#### 💪 核心 8 人"); st.dataframe(format_stats_df(p['df']), hide_index=True)
