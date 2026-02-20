import streamlit as st
import pandas as pd
import requests
import re
import unicodedata
import time
import random
from datetime import datetime, timedelta
import pytz
from bs4 import BeautifulSoup

# --- 1. 核心配置 ---
tw_tz = pytz.timezone('Asia/Taipei')
us_east_tz = pytz.timezone('US/Eastern')

# 模擬極度真實的瀏覽器
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}

TEAM_MAP = {
    'ATL': ['Atlanta Hawks', '老鷹'], 'BKN': ['Brooklyn Nets', '籃網'], 'BOS': ['Boston Celtics', '塞爾提克'],
    'CHA': ['Charlotte Hornets', '黃蜂'], 'CHI': ['Chicago Bulls', '公牛'], 'CLE': ['Cleveland Cavaliers', '騎士'],
    'DAL': ['Dallas Mavericks', '獨行俠'], 'DEN': ['Denver Nuggets', '金塊'], 'DET': ['Detroit Pistons', '活塞'],
    'GSW': ['Golden State Warriors', '勇士'], 'HOU': ['Houston Rockets', '火箭'], 'IND': ['Indiana Pacers', '溜馬'],
    'LAC': ['LA Clippers', '快艇', 'LAC'], 'LAL': ['Los Angeles Lakers', '湖人', 'LAL'], 'MEM': ['Memphis Grizzlies', '灰熊'],
    'MIA': ['Miami Heat', '熱火'], 'MIL': ['Milwaukee Bucks', '公鹿'], 'MIN': ['Minnesota Timberwolves', '灰狼'],
    'NOP': ['New Orleans Pelicans', '鵜鶘'], 'NYK': ['New York Knicks', '尼克'], 'OKC': ['Oklahoma City Thunder', '雷霆'],
    'ORL': ['Orlando Magic', '魔術'], 'PHI': ['Philadelphia 76ers', '76人'], 'PHX': ['Phoenix Suns', '太陽'],
    'POR': ['Portland Trail Blazers', '拓荒者'], 'SAC': ['Sacramento Kings', '國王'], 'SAS': ['San Antonio Spurs', '馬刺'],
    'TOR': ['Toronto Raptors', '暴龍'], 'UTA': ['Utah Jazz', '爵士'], 'WAS': ['Washington Wizards', '巫師']
}

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
    trans = {r'\bOut\b': '❌ 缺陣', r'\bDay-To-Day\b': '📋 每日觀察', r'\bGTD\b': '📋 賽前決定', r'\bQuestionable\b': '🤔 出戰成疑'}
    for eng, chi in trans.items(): text_str = re.sub(eng, chi, text_str, flags=re.IGNORECASE)
    return text_str

# --- 3. 穩定數據引擎 ---

@st.cache_data(ttl=3600)
def load_stable_player_stats():
    """改從 Basketball-Reference 抓取，極其穩定"""
    try:
        url = "https://www.basketball-reference.com/leagues/NBA_2026_per_game.html"
        # 讀取網頁中所有的表格
        dfs = pd.read_html(url)
        df = dfs[0]
        # 清洗數據
        df = df[df['Player'] != 'Player'] # 移除重複表頭
        df['PTS'] = pd.to_numeric(df['PTS'], errors='coerce')
        df['TRB'] = pd.to_numeric(df['TRB'], errors='coerce')
        df['AST'] = pd.to_numeric(df['AST'], errors='coerce')
        df['STL'] = pd.to_numeric(df['STL'], errors='coerce')
        df['BLK'] = pd.to_numeric(df['BLK'], errors='coerce')
        df['TOV'] = pd.to_numeric(df['TOV'], errors='coerce')
        df['FG%'] = pd.to_numeric(df['FG%'], errors='coerce')
        
        # 估算影響力 (IMPACT)
        df['IMPACT'] = df['PTS'] + df['TRB']*1.1 + df['AST']*1.5 + (df['STL']+df['BLK'])*2 - df['TOV']*2
        df['NORMALIZED_NAME'] = df['Player'].apply(normalize_name)
        
        # 轉換隊伍縮寫
        tm_map_ref = {v[0]: k for k, v in TEAM_MAP.items()}
        df['TEAM_ABBR'] = df['Tm'] 
        return df
    except Exception as e:
        st.error(f"無法從備用源獲取數據: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=600)
def get_espn_data():
    """從 ESPN 抓取今日賽程與傷病名單 (目前最穩定的來源)"""
    injuries = []
    games = []
    try:
        # 1. 傷病
        resp = requests.get("https://www.espn.com/nba/injuries", headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for table in soup.select('.ResponsiveTable'):
            t_title = table.select_one('.Table__Title').get_text(strip=True)
            t_abbr = next((a for a, info in TEAM_MAP.items() if any(n.lower() in t_title.lower() for n in info)), "UNK")
            for r in table.select('tbody tr'):
                cols = r.select('td')
                if len(cols) >= 3:
                    p_name = re.sub(r'(PG|SG|SF|PF|C|G|F)$', '', cols[0].get_text(strip=True))
                    status = cols[2].get_text(strip=True)
                    is_out = any(w in status.lower() for w in ['out', 'doubtful'])
                    injuries.append({'Player': p_name, 'NORMALIZED_NAME': normalize_name(p_name), 'Team': t_abbr, 'Status': translate_status(status), 'IS_OUT': is_out})
        
        # 2. 今日賽程 (從 ESPN Scoreboard 抓取)
        resp_sb = requests.get("https://www.espn.com/nba/schedule", headers=HEADERS, timeout=10)
        sb_soup = BeautifulSoup(resp_sb.text, 'html.parser')
        for row in sb_soup.select('.Table__TR'):
            teams_in_game = row.select('.Table__Team a')
            if len(teams_in_game) >= 2:
                away_txt = teams_in_game[0].get_text()
                home_txt = teams_in_game[1].get_text()
                a_abbr = next((k for k, v in TEAM_MAP.items() if any(n.lower() in away_txt.lower() for n in v)), "")
                h_abbr = next((k for k, v in TEAM_MAP.items() if any(n.lower() in home_txt.lower() for n in v)), "")
                if a_abbr and h_abbr:
                    games.append({'Away': a_abbr, 'Home': h_abbr})
    except: pass
    return pd.DataFrame(injuries), games

# --- 4. 主程式 UI ---
st.set_page_config(page_title="NBA Edge 專家 v14.0 (備用源模式)", layout="wide")
st.title("🏀 NBA 數據預測 v14.0 (連線修復版本)")

stats_db = load_stable_player_stats()
injury_df, today_games = get_espn_data()

if stats_db.empty:
    st.error("⚠️ 偵測到網路封鎖，無法獲取基礎數據。請確認您的網路連線或嘗試更換網路環境。")
    st.stop()

if not today_games:
    st.warning("📅 今日暫無賽程數據（或抓取受阻）。")
else:
    st.markdown(f"### 📅 今日推薦：{datetime.now(tw_tz).strftime('%Y-%m-%d')}")
    
    analysis_results = []
    
    for game in today_games:
        h_abbr, a_abbr = game['Home'], game['Away']
        
        def process_team(abbr):
            # 取得傷病名單
            t_inj = injury_df[injury_df['Team'] == abbr] if not injury_df.empty else pd.DataFrame()
            out_names = t_inj[t_inj['IS_OUT']]['NORMALIZED_NAME'].tolist()
            
            # **關鍵：只抓取會上場的球員** (排除 Out)
            active_ps = stats_db[(stats_db['TEAM_ABBR'] == abbr) & (~stats_db['NORMALIZED_NAME'].isin(out_names))].sort_values('IMPACT', ascending=False)
            
            # 戰力計算 (基於上場球員的得分與影響力)
            total_pts = active_ps['PTS'].sum()
            avg_impact = active_ps['IMPACT'].mean()
            
            # 穩定度評估
            gtd_count = len(t_inj[t_inj['Status'].str.contains('📋|🤔', na=False)])
            score = 90 if gtd_count == 0 else (60 if gtd_count == 1 else 30)
            
            return {'pts': total_pts, 'impact': avg_impact, 'df': active_ps, 'inj': t_inj, 'score': score}

        h_data = process_team(h_abbr)
        a_data = process_team(a_abbr)
        
        # 預測模型：結合場均得分差與影響力權重
        diff = (h_data['pts'] - a_data['pts']) * 0.1 + (h_data['impact'] - a_data['impact']) * 5 + 3.0 # +3.0 為主場優勢
        prob = 1 / (1 + 10**(-abs(diff)/10)) * 100
        
        analysis_results.append({
            'home': h_abbr, 'away': a_abbr, 'h_cn': TEAM_MAP[h_abbr][1], 'a_cn': TEAM_MAP[a_abbr][1],
            'diff': diff, 'prob': prob, 'h_data': h_data, 'a_data': a_data
        })

    # --- 挑選過盤率最高 Top 4 ---
    top_4 = sorted(analysis_results, key=lambda x: x['prob'], reverse=True)[:4]
    
    cols = st.columns(min(len(top_4), 2))
    for i, game in enumerate(top_4):
        with cols[i % 2]:
            with st.container(border=True):
                st.subheader(f"{game['a_cn']} @ {game['h_cn']}")
                
                # 賠率輸入 (不論有沒有輸入都能跑)
                c1, c2, c3 = st.columns(3)
                u_sp = c1.number_input("讓分", value=0.0, step=0.5, key=f"sp_{i}")
                u_oh = c2.number_input("主賠", value=1.90, step=0.01, key=f"oh_{i}")
                u_oa = c3.number_input("客賠", value=1.90, step=0.01, key=f"oa_{i}")
                
                final_diff = game['diff'] + u_sp
                win_rec = game['h_cn'] if final_diff > 0 else game['a_cn']
                final_prob = game['prob'] if final_diff > 0 else (100 - game['prob'])
                ev = (final_prob/100 * (u_oh if final_diff > 0 else u_oa)) - 1
                
                st.write(f"📈 預期分差：**{game['diff']:+.1f}**")
                st.success(f"🎯 推薦：**{win_rec}** (勝率 {game['prob']:.1f}%)")
                st.write(f"💎 預計期望值 (EV): {ev*100:+.1f}%")
                
                with st.expander("🔍 預計上場名單與數據"):
                    st.write("**主隊會上場成員**")
                    st.table(game['h_data']['df'][['Player', 'PTS', 'FG%', 'IMPACT']].head(10))
                    if not game['h_data']['inj'].empty:
                        st.write("🚑 傷病：", game['h_data']['inj'][['Player', 'Status']])
                    
                    st.divider()
                    st.write("**客隊會上場成員**")
                    st.table(game['a_data']['df'][['Player', 'PTS', 'FG%', 'IMPACT']].head(10))
                    if not game['a_data']['inj'].empty:
                        st.write("🚑 傷病：", game['a_data']['inj'][['Player', 'Status']])

    # 側邊欄總結
    st.sidebar.title("🎯 串關戰情室")
    safe_picks = [g for g in top_4 if g['prob'] > 65]
    if safe_picks:
        st.sidebar.write("🔥 **高勝率組合推薦：**")
        for g in safe_picks:
            st.sidebar.info(f"{g['a_cn']} vs {g['h_cn']} -> 猜 {g['h_cn'] if g['diff'] > 0 else g['a_cn']}")
