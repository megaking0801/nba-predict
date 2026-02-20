import streamlit as st
import pandas as pd
import requests
import re
import unicodedata
import time
from datetime import datetime
import pytz
from bs4 import BeautifulSoup

# --- 1. 配置 ---
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

# --- 2. 工具函數 ---
def normalize_name(name):
    if not isinstance(name, str): return ""
    name = ''.join(c for c in unicodedata.normalize('NFD', name) if unicodedata.category(c) != 'Mn')
    name = name.lower()
    name = re.sub(r'\b(jr\.?|sr\.?|ii|iii|iv)\b', '', name)
    name = re.sub(r'[.\']', '', name)
    return name.strip()

def translate_status(text):
    if not text: return "未知"
    trans = {r'\bOut\b': '❌ 缺陣', r'\bDay-To-Day\b': '📋 觀察', r'\bGTD\b': '📋 賽前決定', r'\bQuestionable\b': '🤔 出戰成疑'}
    for eng, chi in trans.items(): text = re.sub(eng, chi, text, flags=re.IGNORECASE)
    return text

# --- 3. 數據核心 ---

@st.cache_data(ttl=3600)
def load_all_player_stats():
    """從備用源抓取，修正 'Tm' 欄位找不到的問題"""
    url = "https://www.basketball-reference.com/leagues/NBA_2026_per_game.html"
    try:
        # 嘗試解析 HTML
        dfs = pd.read_html(url, flavor='lxml' if 'lxml' else 'html5lib')
        df = dfs[0]
        
        # 移除重複表頭
        df = df[df['Player'] != 'Player'].copy()
        
        # 修正欄位名稱 (有些版本 'Tm' 會變成其他名稱)
        # 尋找包含 Tm 或 Team 的欄位，如果沒找到就用第 5 欄 (通常是 Tm)
        tm_col = next((c for c in df.columns if 'Tm' in c or 'Team' in c), df.columns[4])
        df.rename(columns={tm_col: 'TEAM_ABBR'}, inplace=True)
        
        # 轉型與數值清理
        cols = ['PTS', 'TRB', 'AST', 'STL', 'BLK', 'TOV', 'FG%', 'MP']
        for c in cols:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
        
        # 計算戰力影響力 (IMPACT)
        df['IMPACT'] = df['PTS'] + df['TRB']*1.2 + df['AST']*1.5 + (df['STL']+df['BLK'])*2 - df['TOV']*2
        df['NORMALIZED_NAME'] = df['Player'].apply(normalize_name)
        
        return df
    except Exception as e:
        st.error(f"數據加載出錯: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=600)
def get_live_schedule_and_injuries():
    """從 ESPN 抓取今日賽程與傷病名單"""
    injuries, games = [], []
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        # 1. 抓傷病
        resp = requests.get("https://www.espn.com/nba/injuries", headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for table in soup.select('.ResponsiveTable'):
            t_title = table.select_one('.Table__Title').get_text()
            t_abbr = next((a for a, i in TEAM_MAP.items() if any(n.lower() in t_title.lower() for n in i)), "UNK")
            for r in table.select('tbody tr'):
                cols = r.select('td')
                if len(cols) >= 3:
                    p_name = re.sub(r'(PG|SG|SF|PF|C|G|F)$', '', cols[0].get_text())
                    status = cols[2].get_text()
                    is_out = any(w in status.lower() for w in ['out', 'doubtful', 'injured'])
                    injuries.append({'Player': p_name, 'NORM': normalize_name(p_name), 'Team': t_abbr, 'Status': translate_status(status), 'IS_OUT': is_out})
        
        # 2. 抓賽程
        resp_s = requests.get("https://www.espn.com/nba/schedule", headers=headers, timeout=10)
        s_soup = BeautifulSoup(resp_s.text, 'html.parser')
        for row in s_soup.select('.Table__TR'):
            links = row.select('.Table__Team a')
            if len(links) >= 2:
                a_name, h_name = links[0].get_text(), links[1].get_text()
                a_abbr = next((k for k, v in TEAM_MAP.items() if any(n.lower() in a_name.lower() for n in v)), "")
                h_abbr = next((k for k, v in TEAM_MAP.items() if any(n.lower() in h_name.lower() for n in v)), "")
                if a_abbr and h_abbr:
                    games.append({'Away': a_abbr, 'Home': h_abbr})
    except: pass
    return pd.DataFrame(injuries), games

# --- 4. 主程式 ---
st.set_page_config(page_title="NBA Edge v14.2", layout="wide")
st.title("🏀 NBA 全員數據預測系統 (修復版)")

df_all = load_all_player_stats()
df_inj, schedule = get_live_schedule_and_injuries()

if df_all.empty:
    st.error("❌ 基礎數據載入失敗。請檢查網路或 requirements.txt 是否包含 lxml。")
    st.stop()

if not schedule:
    st.info("📅 今日目前無賽程資訊。")
else:
    all_game_analysis = []
    
    for g in schedule:
        h_abbr, a_abbr = g['Home'], g['Away']
        
        def get_team_stats(abbr):
            # 取得傷病名單
            team_inj = df_inj[df_inj['Team'] == abbr] if not df_inj.empty else pd.DataFrame()
            outs = team_inj[team_inj['IS_OUT']]['NORM'].tolist()
            
            # 【重要】過濾：只抓會上場的人 (排除缺陣者)
            # Basketball-Reference 的縮寫有時不同，這裡用包含匹配 (例如 'LAL' 匹配 'LAL')
            active = df_all[(df_all['TEAM_ABBR'].str.contains(abbr, case=False, na=False)) & (~df_all['NORMALIZED_NAME'].isin(outs))]
            
            # 如果抓不到人，可能是縮寫不匹配，嘗試二次匹配 (針對部分隊伍)
            if active.empty:
                active = df_all[df_all['TEAM_ABBR'].isin(TEAM_MAP[abbr]) & (~df_all['NORMALIZED_NAME'].isin(outs))]

            return {
                'pts': active['PTS'].sum(),
                'impact': active['IMPACT'].mean(),
                'df': active.sort_values('IMPACT', ascending=False),
                'inj': team_inj
            }

        h_stats = get_team_stats(h_abbr)
        a_stats = get_team_stats(a_abbr)
        
        # 預測核心公式
        score_diff = (h_stats['pts'] - a_stats['pts']) * 0.12 + (h_stats['impact'] - a_stats['impact']) * 5.5 + 2.5
        win_prob = 1 / (1 + 10**(-abs(score_diff)/11)) * 100
        
        all_game_analysis.append({
            'home': h_abbr, 'away': a_abbr, 'h_cn': TEAM_MAP[h_abbr][1], 'a_cn': TEAM_MAP[a_abbr][1],
            'diff': score_diff, 'prob': win_prob, 'h_stats': h_stats, 'a_stats': a_stats
        })

    # --- Top 4 推薦邏輯 ---
    # 推薦勝率最高的前四場，如果不足四場則全顯示
    top_picks = sorted(all_game_analysis, key=lambda x: x['prob'], reverse=True)[:4]
    
    st.header(f"🔥 今日 Top {len(top_picks)} 推薦 (過盤率最高)")
    
    cols = st.columns(min(len(top_picks), 2))
    for i, game in enumerate(top_picks):
        with cols[i % 2]:
            with st.container(border=True):
                st.subheader(f"{game['a_cn']} @ {game['h_cn']}")
                
                # 輸入區
                grid_in = st.columns(3)
                u_sp = grid_in[0].number_input("讓分", value=0.0, step=0.5, key=f"sp_{i}")
                u_oh = grid_in[1].number_input("主賠", value=1.90, step=0.01, key=f"oh_{i}")
                u_oa = grid_in[2].number_input("客賠", value=1.90, step=0.01, key=f"oa_{i}")
                
                # 計算最終推薦
                final_margin = game['diff'] + u_sp
                rec_team = game['h_cn'] if final_margin > 0 else game['a_cn']
                final_prob = game['prob'] if final_margin > 0 else (100 - game['prob'])
                ev = (final_prob/100 * (u_oh if final_margin > 0 else u_oa)) - 1
                
                st.success(f"🎯 推薦：**{rec_team}**")
                st.write(f"📈 勝率：**{final_prob:.1f}%** | EV：**{ev*100:+.1f}%**")
                st.caption(f"基礎分差預測：{game['diff']:+.1f}")
                
                with st.expander("🔍 檢視預計上場全員名單"):
                    c_h, c_a = st.columns(2)
                    with c_h:
                        st.write(f"**{game['h_cn']} 可用球員**")
                        st.dataframe(game['h_stats']['df'][['Player', 'PTS', 'IMPACT']].head(12), hide_index=True)
                        if not game['h_stats']['inj'].empty:
                            st.caption("🚑 傷病名單：")
                            st.dataframe(game['h_stats']['inj'][['Player', 'Status']], hide_index=True)
                    with c_a:
                        st.write(f"**{game['a_cn']} 可用球員**")
                        st.dataframe(game['a_stats']['df'][['Player', 'PTS', 'IMPACT']].head(12), hide_index=True)
                        if not game['a_stats']['inj'].empty:
                            st.caption("🚑 傷病名單：")
                            st.dataframe(game['a_stats']['inj'][['Player', 'Status']], hide_index=True)

    # 側邊欄：串關助手
    if len(top_picks) >= 2:
        st.sidebar.title("💎 串關黃金組合")
        st.sidebar.info(f"建議：{top_picks[0]['a_cn']}@{top_picks[0]['h_cn']} + {top_picks[1]['a_cn']}@{top_picks[1]['h_cn']}")
