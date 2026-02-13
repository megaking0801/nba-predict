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
    
    # 針對 ESPN 顯示日期 (如 Feb 19) 的情況，直接視為缺陣
    if re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+', text_str, re.IGNORECASE):
        return f"❌ 缺陣 (預計 {text_str} 歸隊)"
    
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
        r = endpoint(**kwargs).get_dict()
        res = r['resultSets'][0]
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

def evaluate_stability(inj_df, top_players_names):
    # 判定標準：核心球員是否在每日觀察名單
    gtd_list = ['📋', '🤔', 'GTD', 'Day-To-Day', 'Questionable']
    
    # 檢查是否有人是 GTD
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
    if df.empty: return df
    cols = ['PLAYER_NAME', 'PTS', 'FG_PCT', 'FG3_PCT', 'FT_PCT', 'REB', 'AST', 'PIE']
    d_df = df[[c for c in cols if c in df.columns]].copy()
    # 格式化百分比
    for c in ['FG_PCT', 'FG3_PCT', 'FT_PCT']:
        if c in d_df.columns:
            d_df[c] = (d_df[c] * 100).round(1).astype(str) + '%'
    # 格式化數字
    for c in ['PTS', 'REB', 'AST', 'PIE']:
        if c in d_df.columns:
            d_df[c] = d_df[c].round(1)
    return d_df

# --- 2. 數據抓取引擎 ---
@st.cache_data(ttl=600)
def get_espn_injuries_v30():
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
                    # 抓取原始狀態文字
                    raw_status = cols[2].get_text(strip=True)
                    # 檢查是否有額外說明欄位
                    raw_comment = cols[3].get_text(strip=True) if len(cols) > 3 else ""
                    
                    full_check = (raw_status + " " + raw_comment).lower()
                    # 判斷是否缺陣：包含 out, injured, 或看起來像日期
                    is_date_format = bool(re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+', raw_status, re.IGNORECASE))
                    is_out = any(word in full_check for word in ['out', 'doubtful', 'injured', '❌']) or is_date_format
                    
                    all_inj.append({
                        '球員': p_name, 'NORMALIZED_NAME': normalize_name(p_name),
                        '位置': cols[1].get_text(strip=True),
                        '狀態': translate_status(raw_status), # 使用新的翻譯邏輯
                        '說明': raw_comment, '球隊': t_abbr, 'IS_OUT': is_out
                    })
    except: pass
    return pd.DataFrame(all_inj)

@st.cache_data(ttl=3600)
def load_nba_stats_v30():
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
st.set_page_config(page_title="NBA Edge 專家 v13.30", layout="wide")
st.title("🏀 NBA 數據預測 v13.30 (全功能修復版)")

ps_db = load_nba_stats_v30()
injury_df = get_espn_injuries_v30()
all_season_games = get_all_season_games()

# --- 智慧日期搜尋 (跳過明星賽/表演賽) ---
nba_today = datetime.now(us_east_tz)
target_date = nba_today
sb = pd.DataFrame()
found_regular_games = False

# 搜尋 14 天
for i in range(14):
    current_search_date = nba_today + timedelta(days=i)
    formatted_date = current_search_date.strftime('%m/%d/%Y')
    temp_sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=formatted_date)
    
    if not temp_sb.empty:
        # 檢查是否有正規球隊
        regular_season_games = temp_sb[temp_sb['HOME_TEAM_ID'].isin(VALID_TEAM_IDS)]
        if not regular_season_games.empty:
            sb = regular_season_games
            target_date = current_search_date
            found_regular_games = True
            break

id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}

if not found_regular_games:
    st.error("⚠️ 未來 14 天內未偵測到正規賽季賽程 (可能正處於明星週休兵期)。")
else:
    target_date_str = target_date.strftime('%Y-%m-%d')
    days_diff = (target_date.date() - nba_today.date()).days
    
    st.markdown(f"### 📅 分析日期：{target_date_str} (美東時間)")
    if days_diff > 0:
        st.info(f"💡 今日無正規賽，已自動切換至 {days_diff} 天後的最近賽事。")

    all_game_data, rankings = [], []
    st.sidebar.header("🎯 隔夜串關建議")
    
    st.markdown("### 🔥 精選推薦 Top 4")
    top_container = st.container()
    st.divider()

    grid = st.columns(3)
    yesterday_of_target = (target_date - timedelta(days=1)).strftime('%Y-%m-%d')
    
    for idx, row in sb.iterrows():
        h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
        h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
        if not h_abbr or not a_abbr: continue
        
        def get_pkg(tid, abbr, is_b2b):
            t_inj = injury_df[injury_df['球隊'] == abbr]
            out_names = t_inj[t_inj['IS_OUT']]['NORMALIZED_NAME'].tolist()
            all_ps = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
            top_8 = all_ps[~all_ps['NORMALIZED_NAME'].isin(out_names)].head(8)
            status_text, s_score = evaluate_stability(t_inj, top_8['NORMALIZED_NAME'].tolist())
            return {'pts': top_8['PTS'].sum(), 'pie': top_8['PIE'].mean(), 'df': top_8, 'inj': t_inj, 'ex': all_ps[all_ps['NORMALIZED_NAME'].isin(out_names)]['PLAYER_NAME'].tolist(), 'b2b': is_b2b, 'status': status_text, 'score': s_score}

        h_b2b = not all_season_games[(all_season_games['TEAM_ID'] == h_id) & (all_season_games['GAME_DATE'] == yesterday_of_target)].empty if not all_season_games.empty else False
        a_b2b = not all_season_games[(all_season_games['TEAM_ID'] == a_id) & (all_season_games['GAME_DATE'] == yesterday_of_target)].empty if not all_season_games.empty else False
        
        h_pkg, a_pkg = get_pkg(h_id, h_abbr, h_b2b), get_pkg(a_id, a_abbr, a_b2b)
        h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
        
        raw_diff = (h_pkg['pts'] - a_pkg['pts']) * 0.12 + (h_pkg['pie'] - a_pkg['pie']) * 45 + 2.5
        if h_pkg['b2b']: raw_diff -= 1.5
        if a_pkg['b2b']: raw_diff += 1.5
        
        g_key = f"v30_{idx}"
        with grid[idx % 3]:
            with st.container(border=True):
                st.markdown(f"#### {a_cn} @ {h_cn}")
                c_sp, c_h, c_a = st.columns([2, 1, 1])
                u_spread = c_sp.number_input(f"讓分", value=0.0, step=0.5, key=f"sp_{g_key}")
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
                st.write(f"📊 **評級：{stars}**")
                st.write(f"🛡️ 穩定：{avg_stab:.0f}%")
                m1, m2 = st.columns(2)
                m1.metric("Edge", f"{final_edge:.1f}")
                m2.metric("EV", f"{ev*100:+.1f}%")
                
                rankings.append({
                    'matchup': f"{a_cn}@{h_cn}", 'rec_team': rec_team, 
                    'prob': disp_prob, 'edge': final_edge, 'ev': ev, 'stability': avg_stab
                })
        all_game_data.append({'label': f"{a_cn}@{h_cn}", 'h_pkg': h_pkg, 'a_pkg': a_pkg, 'h_cn': h_cn, 'a_cn': a_cn})

    # Top 4 渲染
    rankings.sort(key=lambda x: (x['stability'] > 50, x['ev']), reverse=True)
    with top_container:
        if rankings:
            n_show = min(len(rankings), 4)
            cols = st.columns(n_show)
            for i in range(n_show):
                r = rankings[i]
                icon = "🟢" if r['stability'] > 70 else "🟡" if r['stability'] > 40 else "🔴"
                cols[i].info(f"**TOP {i+1} {icon}**\n\n{r['matchup']}\n\n**{r['rec_team']}**\n🎯 {r['prob']:.1f}% | 📈 EV: {r['ev']*100:+.1f}%")

    with st.sidebar:
        safe_picks = [r for r in rankings if r['stability'] > 70 and r['ev'] > 0.05]
        if len(safe_picks) >= 2:
            st.success("🔥 穩定型 2 串 1")
            st.code(f"{safe_picks[0]['rec_team']} + {safe_picks[1]['rec_team']}")
        else: st.warning("名單變數大，建議觀望")

    st.divider()
    st.markdown("### 🔍 對戰詳細數據比較 (含傷病 & 核心戰力)")
    if all_game_data:
        sel_game = st.selectbox("選擇對戰組合", [g['label'] for g in all_game_data])
        curr = next((g for g in all_game_data if g['label'] == sel_game), None)
        if curr:
            # Layout: 左欄主隊，右欄客隊
            col_main_h, col_main_a = st.columns(2)
            
            with col_main_h:
                st.subheader(f"[主] {curr['h_cn']}")
                st.caption(f"穩定度評估: {curr['h_pkg']['status']}")
                st.markdown("**🚑 傷病名單**")
                st.dataframe(curr['h_pkg']['inj'][['球員', '狀態', '說明']] if not curr['h_pkg']['inj'].empty else pd.DataFrame(columns=['✅ 全員健康']), hide_index=True, use_container_width=True)
                
                st.markdown("**💪 核心戰力 (前 8 人)**")
                st.dataframe(format_stats_df(curr['h_pkg']['df']), hide_index=True, use_container_width=True)

            with col_main_a:
                st.subheader(f"[客] {curr['a_cn']}")
                st.caption(f"穩定度評估: {curr['a_pkg']['status']}")
                st.markdown("**🚑 傷病名單**")
                st.dataframe(curr['a_pkg']['inj'][['球員', '狀態', '說明']] if not curr['a_pkg']['inj'].empty else pd.DataFrame(columns=['✅ 全員健康']), hide_index=True, use_container_width=True)
                
                st.markdown("**💪 核心戰力 (前 8 人)**")
                st.dataframe(format_stats_df(curr['a_pkg']['df']), hide_index=True, use_container_width=True)
