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
    return name.strip()

def translate_text(text):
    if not text or pd.isna(text): return ""
    res = str(text)
    trans = {
        r'\bOut\b': '❌ 缺陣', r'\bDay-To-Day\b': '📋 每日觀察', r'\bGTD\b': '📋 賽前決定',
        r'\bQuestionable\b': '🤔 出戰成疑', r'\bDoubtful\b': '😰 極大機率缺陣', r'\bProbable\b': '✅ 可能出戰'
    }
    for eng, chi in trans.items():
        res = re.sub(eng, chi, res, flags=re.IGNORECASE)
    return res

def fetch_safe_df(endpoint, **kwargs):
    try:
        r = endpoint(**kwargs).get_dict()
        res = r['resultSets'][0]
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except Exception as e:
        return pd.DataFrame()

# --- 2. 數據抓取引擎 ---
@st.cache_data(ttl=600)
def get_espn_injuries_v18():
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
def load_nba_stats_v18():
    S = '2025-26'
    p_base = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    p_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    if p_base.empty: return pd.DataFrame()
    cols_to_add = ['PLAYER_ID', 'TS_PCT', 'PIE']
    if not p_adv.empty:
        p_full = pd.merge(p_base, p_adv[cols_to_add], on='PLAYER_ID', how='left')
    else:
        p_full = p_base
        p_full['TS_PCT'] = 0; p_full['PIE'] = 0
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    p_full['NORMALIZED_NAME'] = p_full['PLAYER_NAME'].apply(normalize_name)
    return p_full

# --- 3. UI 顯示邏輯 ---
st.set_page_config(page_title="NBA 數據專家 v13.18", layout="wide")
st.title("🏀 NBA 數據專家 v13.18 (穩定強化版)")

ps_db = load_nba_stats_v18()
injury_df = get_espn_injuries_v18()

nba_now = datetime.now(us_east_tz)
sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=nba_now.strftime('%m/%d/%Y'))
id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}

# [優化] 在循環外只抓一次全賽季歷史資料，避免頻繁請求 API
@st.cache_data(ttl=1800)
def get_all_season_games():
    return fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable='2025-26')

all_season_games = get_all_season_games()

if sb.empty:
    st.info("📅 今日暫無比賽排程")
else:
    all_game_data = [] 
    rankings = []
    
    st.markdown("### 🔥 今日精選 Top 4 (過盤率最高)")
    top_container = st.container()
    st.divider()

    st.markdown("### 🏟️ 所有賽程與盤口輸入")
    grid = st.columns(3)
    
    yesterday = (nba_now - timedelta(days=1)).strftime('%Y-%m-%d')
    
    for idx, row in sb.iterrows():
        h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
        h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
        if not h_abbr or not a_abbr: continue
        
        # [優化] 使用記憶體內的資料過濾，不再請求網路
        h_b2b = False; a_b2b = False
        if not all_season_games.empty:
            h_b2b = not all_season_games[(all_season_games['TEAM_ID'] == h_id) & (all_season_games['GAME_DATE'] == yesterday)].empty
            a_b2b = not all_season_games[(all_season_games['TEAM_ID'] == a_id) & (all_season_games['GAME_DATE'] == yesterday)].empty

        def get_pkg(tid, abbr, is_b2b):
            t_inj = injury_df[injury_df['球隊'] == abbr]
            out_list = t_inj[t_inj['IS_OUT']]['NORMALIZED_NAME'].tolist()
            all_ps = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
            active = all_ps[~all_ps['NORMALIZED_NAME'].isin(out_list)].head(8)
            return {'pts': active['PTS'].sum(), 'pie': active['PIE'].mean(), 'df': active, 'inj': t_inj, 'ex': all_ps[all_ps['NORMALIZED_NAME'].isin(out_list)]['PLAYER_NAME'].tolist(), 'b2b': is_b2b}

        h_pkg, a_pkg = get_pkg(h_id, h_abbr, h_b2b), get_pkg(a_id, a_abbr, a_b2b)
        h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
        
        raw_diff = (h_pkg['pts'] - a_pkg['pts']) * 0.12 + (h_pkg['pie'] - a_pkg['pie']) * 45 + 2.5
        if h_pkg['b2b']: raw_diff -= 1.5
        if a_pkg['b2b']: raw_diff += 1.5
        
        model_prob_h = 1 / (1 + 10**(-raw_diff/15)) * 100
        g_key = f"v1318_{idx}"
        
        with grid[idx % 3]:
            with st.container(border=True):
                h_b_lbl = "⚡(B2B)" if h_pkg['b2b'] else ""
                a_b_lbl = "⚡(B2B)" if a_pkg['b2b'] else ""
                st.markdown(f"#### [客] {a_cn}{a_b_lbl} @ [主] {h_cn}{h_b_lbl}")
                
                c_sp, c_h, c_a = st.columns([2, 1, 1])
                u_spread = c_sp.number_input(f"[主]{h_cn} 讓分", value=0.0, step=0.5, key=f"sp_{g_key}")
                u_oh, u_oa = c_h.number_input(f"[主]賠", 1.75, key=f"oh_{g_key}"), c_a.number_input(f"[客]賠", 1.75, key=f"oa_{g_key}")
                
                final_prob_h = (model_prob_h * 0.6) + (((1/u_oh)/(1/u_oh+1/u_oa)*100) * 0.4)
                edge = raw_diff + u_spread 
                cover_prob = 1 / (1 + 10**(-edge/8)) * 100
                
                if edge < 0:
                    display_cover_prob = (100 - cover_prob)
                    rec_team = f"[客] {a_cn}"
                else:
                    display_cover_prob = cover_prob
                    rec_team = f"[主] {h_cn}"

                st.divider()
                st.metric(f"[主] {h_cn} 綜合勝率", f"{final_prob_h:.1f}%")
                if edge > 2.0: st.success(f"🔥 推薦: [主] {h_cn} 過盤\n🎯 過盤率: {cover_prob:.1f}%")
                elif edge < -2.0: st.error(f"🔥 推薦: [客] {a_cn} 過盤\n🎯 過盤率: {(100-cover_prob):.1f}%")
                else: st.info("⚖️ 盤口精準")
                
                rankings.append({'matchup': f"{a_cn} vs {h_cn}", 'rec_team': rec_team, 'cover_prob': display_cover_prob, 'spread_val': u_spread})
        
        # [優化] H2H 同樣使用記憶體內資料
        h2h_records = []
        if not all_season_games.empty:
            h2h_df = all_season_games[((all_season_games['TEAM_ID'] == h_id) & (all_season_games['MATCHUP'].str.contains(a_abbr)))].head(3)
            h2h_records = h2h_df[['GAME_DATE', 'MATCHUP', 'WL', 'PLUS_MINUS']].to_dict('records')

        all_game_data.append({
            'label': f"[客]{a_cn} vs [主]{h_cn}", 'h_cn': h_cn, 'a_cn': a_cn,
            'h_pkg': h_pkg, 'a_pkg': a_pkg, 'h2h': h2h_records
        })

    # --- 頂部推薦區 ---
    rankings.sort(key=lambda x: x['cover_prob'], reverse=True)
    top_picks = rankings[:4]
    with top_container:
        if top_picks:
            cols = st.columns(len(top_picks))
            for i, pick in enumerate(top_picks):
                with cols[i]:
                    with st.container(border=True):
                        st.markdown(f"**#{i+1} {pick['matchup']}**")
                        st.metric("推薦", pick['rec_team'])
                        st.markdown(f"🎯 過盤率: **{pick['cover_prob']:.1f}%**")

    # --- 4. 底部詳細數據比較區 (加強穩定性) ---
    st.divider()
    st.markdown("### 🔍 對戰詳細數據比較")
    
    if all_game_data:
        game_labels = [g['label'] for g in all_game_data]
        sel_game = st.selectbox("選擇對戰組合", game_labels)
        
        # [優化] 增加安全性檢查
        curr = next((g for g in all_game_data if g['label'] == sel_game), None)
        
        if curr:
            st.write("⚔️ **本季對戰紀錄 (H2H)**")
            if curr['h2h']: st.dataframe(pd.DataFrame(curr['h2h']), hide_index=True, use_container_width=True)
            else: st.write("本季尚未交手")

            st.divider()
            st.markdown(f"#### 🚑 {sel_game} - 傷病報告")
            i_col1, i_col2 = st.columns(2)
            with i_col1:
                st.write(f"**[主] {curr['h_cn']}**")
                if not curr['h_pkg']['inj'].empty: st.dataframe(curr['h_pkg']['inj'][['球員', '位置', '狀態', 'IS_OUT']], hide_index=True, use_container_width=True)
                else: st.success("✅ 全員健康")
            with i_col2:
                st.write(f"**[客] {curr['a_cn']}**")
                if not curr['a_pkg']['inj'].empty: st.dataframe(curr['a_pkg']['inj'][['球員', '位置', '狀態', 'IS_OUT']], hide_index=True, use_container_width=True)
                else: st.success("✅ 全員健康")

            st.divider()
            st.markdown(f"#### 🛡️ {sel_game} - 核心 8 人戰力")
            p_col1, p_col2 = st.columns(2)
            
            def format_stats(df):
                cols = ['PLAYER_NAME', 'PTS', 'FG_PCT', 'FG3_PCT', 'FT_PCT', 'REB', 'AST', 'PIE']
                avail_cols = [c for c in cols if c in df.columns]
                display_df = df[avail_cols].copy()
                for col in ['FG_PCT', 'FG3_PCT', 'FT_PCT']:
                    if col in display_df.columns: display_df[col] = (display_df[col] * 100).round(1).astype(str) + '%'
                return display_df

            with p_col1:
                st.write(f"**[主] {curr['h_cn']} 核心數據**")
                if curr['h_pkg']['ex']: st.error(f"🚫 已排除缺陣: {', '.join(curr['h_pkg']['ex'])}")
                st.dataframe(format_stats(curr['h_pkg']['df']), hide_index=True, use_container_width=True)
            with p_col2:
                st.write(f"**[客] {curr['a_cn']} 核心數據**")
                if curr['a_pkg']['ex']: st.error(f"🚫 已排除缺陣: {', '.join(curr['a_pkg']['ex'])}")
                st.dataframe(format_stats(curr['a_pkg']['df']), hide_index=True, use_container_width=True)
        else:
            st.warning("⚠️ 無法載入該組合的數據，請嘗試切換回其他組合。")
