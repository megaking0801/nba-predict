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

# 判讀指南 (固定於側欄)
def render_legend():
    st.sidebar.info("""
    **💡 數據判讀指南**
    
    **Edge (讓分優勢)**
    - ⭐ **> 3.0**: 價值投資
    - ⭐⭐ **> 5.0**: 強力推薦
    - *公式：模型預測分差 - 莊家盤口*
    
    **EV (期望值)**
    - 📈 **> 5%**: 值得進場
    - 🔥 **> 10%**: 極佳機會
    
    **穩定度 (Stability)**
    - 🟢 **> 70%**: 核心健康，適合隔夜
    - 🔴 **< 40%**: 變數大，建議臨場
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
    
    date_match = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d+)', text_str, re.IGNORECASE)
    if date_match:
        month_str = date_match.group(1)
        day_str = date_match.group(2)
        months = {'Jan':1, 'Feb':2, 'Mar':3, 'Apr':4, 'May':5, 'Jun':6, 'Jul':7, 'Aug':8, 'Sep':9, 'Oct':10, 'Nov':11, 'Dec':12}
        m_num = months.get(month_str[:3].title(), 0)
        return f"❌ 缺陣 (預計 {m_num}/{day_str} 歸隊)"
    
    trans = {
        r'\bOut\b': '❌ 缺陣', 
        r'\bDay-To-Day\b': '📋 每日觀察', 
        r'\bGTD\b': '📋 賽前決定',
        r'\bQuestionable\b': '🤔 出戰成疑', 
        r'\bDoubtful\b': '😰 極大機率缺陣', 
        r'\bProbable\b': '✅ 可能出戰'
    }
    for eng, chi in trans.items():
        text_str = re.sub(eng, chi, text_str, flags=re.IGNORECASE)
    return text_str

def fetch_safe_df(endpoint, **kwargs):
    try:
        # 加入 timeout 避免 NBA API 無回應導致網頁空白卡死
        r = endpoint(timeout=15, **kwargs).get_dict()
        res = r['resultSets'][0]
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except Exception as e: 
        return pd.DataFrame()

def evaluate_stability(inj_df, top_players_names):
    gtd_list = ['📋', '🤔', 'GTD', 'Day-To-Day', 'Questionable']
    core_gtd = 0
    for _, row in inj_df.iterrows():
        status_check = row['狀態']
        name_check = row['NORMALIZED_NAME']
        if any(x in status_check for x in gtd_list) and name_check in top_players_names:
            core_gtd += 1
            
    if core_gtd >= 2: return "🔴 極高風險 (核心變數大)", 20
    if core_gtd == 1: return "🟡 中度風險 (建議觀望)", 60
    return "🟢 穩定性高 (適合隔夜)", 95

def get_edge_stars(edge, ev, stability_score):
    base_stars = 0
    if edge > 5.5 and ev > 0.1: base_stars = 5
    elif edge > 3.5 and ev > 0.05: base_stars = 4
    elif edge > 1.5 and ev > 0: base_stars = 3
    else: base_stars = 2
    if stability_score < 50: base_stars = min(base_stars, 2)
    return "⭐" * base_stars

def format_stats_df(df):
    if df is None or df.empty: return pd.DataFrame()
    cols = ['PLAYER_NAME', 'PTS', 'FG_PCT', 'FG3_PCT', 'REB', 'AST', 'PIE']
    d_df = df[[c for c in cols if c in df.columns]].copy()
    rename_map = {'PLAYER_NAME':'球員', 'PTS':'得分', 'FG_PCT':'命中%', 'FG3_PCT':'三分%', 'REB':'籃板', 'AST':'助攻', 'PIE':'貢獻值'}
    d_df.rename(columns=rename_map, inplace=True)
    
    for c in ['命中%', '三分%']:
        if c in d_df.columns:
            d_df[c] = (d_df[c] * 100).round(1).astype(str) + '%'
    for c in ['得分', '籃板', '助攻', '貢獻值']:
        if c in d_df.columns:
            d_df[c] = d_df[c].round(1)
    return d_df

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
                    raw_status = cols[2].get_text(strip=True)
                    raw_comment = cols[3].get_text(strip=True) if len(cols) > 3 else ""
                    
                    full_check = (raw_status + " " + raw_comment).lower()
                    is_date = bool(re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+', raw_status, re.IGNORECASE))
                    is_out = any(word in full_check for word in ['out', 'doubtful', 'injured', '❌']) or is_date
                    
                    all_inj.append({
                        '球員': p_name, 'NORMALIZED_NAME': normalize_name(p_name),
                        '位置': cols[1].get_text(strip=True),
                        '狀態': translate_status(raw_status),
                        '說明': raw_comment, '球隊': t_abbr, 'IS_OUT': is_out
                    })
    except: pass
    return pd.DataFrame(all_inj)

@st.cache_data(ttl=3600)
def load_nba_stats_v31():
    S = '2025-26'
    p_base = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    p_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    if p_base.empty: return pd.DataFrame()
    p_full = pd.merge(p_base, p_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID', how='left')
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    p_full['NORMALIZED_NAME'] = p_full['PLAYER_NAME'].apply(normalize_name)
    return p_full

@st.cache_data(ttl=1800)
def get_all_season_games():
    return fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable='2025-26')

# --- 3. UI 主架構 ---
st.set_page_config(page_title="NBA Edge 專家 v13.31", layout="wide")
render_legend()
st.title("🏀 NBA 數據預測 v13.31 (資訊補完版)")

ps_db = load_nba_stats_v31()
injury_df = get_espn_injuries_v31()
all_season_games = get_all_season_games()

# 檢查資料庫是否因為 API 問題為空
if ps_db.empty:
    st.error("⚠️ 無法連線至 NBA API 獲取球員數據，請稍後再試。")
    st.stop()

# --- 智慧日期搜尋 ---
nba_today = datetime.now(us_east_tz)
target_date = nba_today
sb = pd.DataFrame()
found_regular_games = False

for i in range(14):
    current_search_date = nba_today + timedelta(days=i)
    formatted_date = current_search_date.strftime('%m/%d/%Y')
    temp_sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=formatted_date)
    if not temp_sb.empty:
        if not temp_sb[temp_sb['HOME_TEAM_ID'].isin(VALID_TEAM_IDS)].empty:
            sb = temp_sb[temp_sb['HOME_TEAM_ID'].isin(VALID_TEAM_IDS)]
            target_date = current_search_date
            found_regular_games = True
            break

id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}

if not found_regular_games:
    st.error("⚠️ 未來 14 天內未偵測到正規賽季賽程。")
else:
    target_date_str = target_date.strftime('%Y-%m-%d')
    days_diff = (target_date.date() - nba_today.date()).days
    
    st.markdown(f"### 📅 分析日期：{target_date_str} (美東時間)")
    if days_diff > 0:
        st.info(f"💡 跳過無賽事日期，已鎖定 {days_diff} 天後的比賽。")

    st.sidebar.header("🎯 隔夜串關建議")
    st.markdown("### 🔥 賽事推薦分析 (Top 4)")
    st.divider()

    yesterday_of_target = (target_date - timedelta(days=1)).strftime('%Y-%m-%d')
    
    # --- 預先計算與排序階段 (抽出 Top 4) ---
    games_analysis = []
    
    for idx, row in sb.iterrows():
        h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
        h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
        if not h_abbr or not a_abbr: continue
        
        def get_pkg(tid, abbr, is_b2b):
            t_inj = injury_df[injury_df['球隊'] == abbr] if not injury_df.empty else pd.DataFrame()
            out_names = t_inj[t_inj['IS_OUT']]['NORMALIZED_NAME'].tolist() if not t_inj.empty else []
            all_ps = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
            
            # 取出【所有】會上場的球員進行分析
            active_players = all_ps[~all_ps['NORMALIZED_NAME'].isin(out_names)]
            
            # 預防資料空值產生的計算錯誤
            pts_sum = active_players['PTS'].sum() if not active_players.empty else 0.0
            pie_mean = active_players['PIE'].mean() if not active_players.empty else 0.0
            
            # 穩定度依舊以核心前8人作為評判基準
            core_names = active_players.head(8)['NORMALIZED_NAME'].tolist()
            status_text, s_score = evaluate_stability(t_inj, core_names)
            
            return {
                'pts': pts_sum, 'pie': pie_mean, 'df': active_players, 
                'inj': t_inj, 'ex': all_ps[all_ps['NORMALIZED_NAME'].isin(out_names)]['PLAYER_NAME'].tolist(), 
                'b2b': is_b2b, 'status': status_text, 'score': s_score
            }

        h_b2b = not all_season_games[(all_season_games['TEAM_ID'] == h_id) & (all_season_games['GAME_DATE'] == yesterday_of_target)].empty if not all_season_games.empty else False
        a_b2b = not all_season_games[(all_season_games['TEAM_ID'] == a_id) & (all_season_games['GAME_DATE'] == yesterday_of_target)].empty if not all_season_games.empty else False
        
        h_pkg, a_pkg = get_pkg(h_id, h_abbr, h_b2b), get_pkg(a_id, a_abbr, a_b2b)
        h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
        
        # 基礎預測邏輯
        raw_diff = (h_pkg['pts'] - a_pkg['pts']) * 0.12 + (h_pkg['pie'] - a_pkg['pie']) * 45 + 2.5
        if h_pkg['b2b']: raw_diff -= 1.5
        if a_pkg['b2b']: raw_diff += 1.5
        
        # 計算不受賠率影響的「基礎過盤勝率」，用作 Top 4 排名依據
        base_edge_val = abs(raw_diff) if pd.notna(raw_diff) else 0
        base_cover_prob = 1 / (1 + 10**(-base_edge_val/8)) * 100
        
        games_analysis.append({
            'idx': idx, 'h_cn': h_cn, 'a_cn': a_cn, 'raw_diff': raw_diff,
            'base_prob': base_cover_prob, 'h_pkg': h_pkg, 'a_pkg': a_pkg,
            'label': f"{a_cn} (客) @ {h_cn} (主)"
        })

    # 排序並取出過盤率最高的前四場 (若不足四場則全取)
    games_analysis = sorted(games_analysis, key=lambda x: x['base_prob'], reverse=True)
    top_4_games = games_analysis[:4]
    
    # --- UI 渲染階段 ---
    grid = st.columns(3)
    rankings = []
    
    for i, game in enumerate(top_4_games):
        h_cn, a_cn = game['h_cn'], game['a_cn']
        raw_diff = game['raw_diff']
        h_pkg, a_pkg = game['h_pkg'], game['a_pkg']
        g_key = f"v31_{game['idx']}"
        
        with grid[i % 3]:
            with st.container(border=True):
                st.markdown(f"#### 🏀 {a_cn} (客) @ {h_cn} (主)")
                
                c_sp, c_h, c_a = st.columns([2, 1, 1])
                # 使用者仍可輸入賠率，如果沒輸入，預設值也能讓它算出結果
                u_spread = c_sp.number_input(f"讓分 (+主/-客)", value=0.0, step=0.5, key=f"sp_{g_key}")
                u_oh = c_h.number_input(f"主賠", 1.01, 5.0, 1.90, 0.01, key=f"oh_{g_key}")
                u_oa = c_a.number_input(f"客賠", 1.01, 5.0, 1.90, 0.01, key=f"oa_{g_key}")
                
                edge_val = raw_diff + u_spread 
                cover_prob = 1 / (1 + 10**(-edge_val/8)) * 100
                
                if edge_val >= 0: disp_prob, rec_team, sel_odds = cover_prob, h_cn, u_oh
                else: disp_prob, rec_team, sel_odds = (100-cover_prob), a_cn, u_oa
                
                ev = (disp_prob/100 * sel_odds) - 1
                final_edge = abs(edge_val)
                avg_stab = (h_pkg['score'] + a_pkg['score']) / 2
                stars = get_edge_stars(final_edge, ev, avg_stab)

                st.divider()
                if avg_stab > 70:
                    st.success(f"🔥 **推薦：{rec_team}**")
                else:
                    st.warning(f"⚠️ **推薦：{rec_team} (風險高)**")
                
                c1, c2 = st.columns(2)
                c1.write(f"**勝率：{disp_prob:.1f}%**")
                c2.write(f"**EV：{ev*100:+.1f}%**")
                
                st.write(f"📊 評級：{stars}")
                st.write(f"Edge：{final_edge:.1f}")

                rankings.append({
                    'matchup': f"{a_cn}@{h_cn}", 'rec_team': rec_team, 
                    'prob': disp_prob, 'edge': final_edge, 'ev': ev, 'stability': avg_stab
                })

    with st.sidebar:
        safe_picks = [r for r in rankings if r['stability'] > 70 and r['ev'] > 0.05]
        if len(safe_picks) >= 2:
            st.success("🔥 穩定型 2 串 1")
            st.code(f"{safe_picks[0]['rec_team']} + {safe_picks[1]['rec_team']}")
        else: 
            st.warning("名單變數大，建議觀望")

    st.divider()
    st.markdown("### 🔍 對戰詳細數據比較 (含傷病 & 完整上場戰力)")
    if top_4_games:
        sel_game_label = st.selectbox("選擇對戰組合", [g['label'] for g in top_4_games])
        curr = next((g for g in top_4_games if g['label'] == sel_game_label), None)
        
        if curr:
            col_main_h, col_main_a = st.columns(2)
            
            with col_main_h:
                st.header(f"🏠 [主] {curr['h_cn']}")
                st.caption(f"穩定度評估: {curr['h_pkg']['status']}")
                st.markdown("#### 🚑 傷病名單")
                st.dataframe(curr['h_pkg']['inj'][['球員', '狀態', '說明']] if not curr['h_pkg']['inj'].empty else pd.DataFrame(columns=['✅ 全員健康']), hide_index=True, use_container_width=True)
                
                st.markdown("#### 💪 預計上場戰力 (完整名單)")
                st.dataframe(format_stats_df(curr['h_pkg']['df']), hide_index=True, use_container_width=True)

            with col_main_a:
                st.header(f"✈️ [客] {curr['a_cn']}")
                st.caption(f"穩定度評估: {curr['a_pkg']['status']}")
                st.markdown("#### 🚑 傷病名單")
                st.dataframe(curr['a_pkg']['inj'][['球員', '狀態', '說明']] if not curr['a_pkg']['inj'].empty else pd.DataFrame(columns=['✅ 全員健康']), hide_index=True, use_container_width=True)
                
                st.markdown("#### 💪 預計上場戰力 (完整名單)")
                st.dataframe(format_stats_df(curr['a_pkg']['df']), hide_index=True, use_container_width=True)
