import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats
)
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests, re
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

# 深度翻譯與位置轉換
TRANS_DICT = {
    r'\bOut\b': '❌ 缺陣',
    r'\bDay-To-Day\b': '📋 每日觀察',
    r'\bGTD\b': '📋 賽前決定',
    r'\bQuestionable\b': '🤔 出戰成疑',
    r'\bDoubtful\b': '😰 極大機率缺陣',
    r'\bProbable\b': '✅ 可能出戰',
    r'\bG\b': '後衛', r'\bF\b': '前鋒', r'\bC\b': '中鋒', # 針對截圖問題的翻譯
    r'\bPG\b': '控衛', r'\bSG\b': '得分衛', r'\bSF\b': '小前鋒', r'\bPF\b': '大前鋒',
    'Achilles': '阿基里斯腱', 'Knee': '膝蓋', 'Ankle': '腳踝', 'Foot': '腳部',
    'Back': '背部', 'Shoulder': '肩膀', 'Wrist': '手腕', 'Surgery': '手術'
}

def clean_description(text):
    if not text or pd.isna(text): return "尚無細節"
    # 移除贅詞並翻譯
    text = re.sub(r'(is|was|has been) (expected to|slated to|ruled|out for).*?(the|until)', '', str(text), flags=re.IGNORECASE)
    # 取第一句重點
    summary = str(text).split('.')[0]
    return translate_text(summary)

def translate_text(text):
    if not text or pd.isna(text): return ""
    res = str(text)
    for eng, chi in TRANS_DICT.items():
        res = re.sub(eng, chi, res, flags=re.IGNORECASE)
    return res

st.set_page_config(page_title="NBA 數據專家 v13.6", layout="wide")

# --- 2. 數據引擎 ---
@st.cache_data(ttl=600)
def get_espn_injuries_v4():
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'}
    all_inj = []
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        tables = soup.select('.ResponsiveTable')
        for table in tables:
            title_node = table.select_one('.Table__Title')
            if not title_node: continue
            t_name = title_node.get_text(strip=True)
            t_abbr = next((a for a, info in TEAM_MAP.items() if any(n.lower() in t_name.lower() for n in info)), "UNK")
            
            rows = table.select('tbody tr')
            for r in rows:
                cols = r.select('td')
                if len(cols) >= 3:
                    # 修正截圖中的錯位問題
                    p_name = re.sub(r'(PG|SG|SF|PF|C|G|F)$', '', cols[0].get_text(strip=True))
                    pos_raw = cols[1].get_text(strip=True) # G, F, C
                    status_raw = cols[2].get_text(strip=True) # Out, GTD...
                    desc_raw = cols[3].get_text(strip=True) if len(cols) > 3 else "No details"

                    all_inj.append({
                        '球員': p_name,
                        '位置': translate_text(pos_raw),
                        '狀態': translate_text(status_raw),
                        '說明': clean_description(desc_raw),
                        '球隊': t_abbr,
                        'RAW_STATUS': status_raw
                    })
    except: pass
    return pd.DataFrame(all_inj)

@st.cache_data(ttl=3600)
def load_all_nba_stats():
    S = '2025-26'
    p_base = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    p_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    if p_base.empty: return pd.DataFrame(), {}, "N/A"
    p_full = pd.merge(p_base, p_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID', how='left')
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    gf = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    l10 = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(10, min_periods=3).mean()).groupby(gf['TEAM_ID']).last().to_dict() if not gf.empty else {}
    return p_full, l10, datetime.now(tw_tz).strftime("%H:%M")

def fetch_safe_df(endpoint, **kwargs):
    try:
        r = endpoint(**kwargs).get_dict()
        res = r['resultSets'][0] if 'resultSets' in r else r['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

# --- 數據同步 ---
ps_db, l10_db, update_time = load_all_nba_stats()
injury_df = get_espn_injuries_v4()

# --- 3. UI 顯示 ---
st.title("🏀 NBA 數據專家 v13.6 (主客標籤與讓分修正)")
st.sidebar.write(f"📊 最後同步: {update_time}")

nba_now = datetime.now(us_east_tz)
dates = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 暫無比賽排程"); continue
        id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        results = []
        cols = st.columns(3)
        for idx, row in sb.iterrows():
            h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
            h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
            if not h_abbr or not a_abbr: continue
            
            def build_team_package(tid, abbr):
                t_inj = injury_df[injury_df['球隊'] == abbr]
                out_names = t_inj[t_inj['RAW_STATUS'].str.contains('Out|Doubtful', case=False, na=False)]['球員'].tolist()
                all_ps = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
                active_core = all_ps[~all_ps['PLAYER_NAME'].apply(lambda x: any(name in x for name in out_names))].head(8)
                return {'pts': active_core['PTS'].sum(), 'ts': active_core['TS_PCT'].mean(), 'pie': active_core['PIE'].mean(), 'df': active_core, 'inj_df': t_inj}

            h_res, a_res = build_team_package(h_id, h_abbr), build_team_package(a_id, a_abbr)
            # 勝率與讓分計算
            diff = (h_res['pts']-a_res['pts'])*0.12 + (h_res['ts']-a_res['ts'])*15 + (h_res['pie']-a_res['pie'])*45 + (l10_db.get(h_id,0)-l10_db.get(a_id,0))*0.4 + 2.5
            prob_h = 1 / (1 + 10**(-diff/15)) * 100
            pred_spread = -diff # 預估讓分 (負數代表主讓)

            h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
            g_key = f"v136_{dates[i].strftime('%Y%m%d')}_{a_abbr}_{h_abbr}"
            
            with cols[idx % 3]:
                with st.container(border=True):
                    # 加上明確的主客標籤
                    st.markdown(f"### [客] {a_cn} vs [主] {h_cn}")
                    st.metric(f"{h_cn} [主] 勝率", f"{prob_h:.1f}%")
                    st.metric(f"{a_cn} [客] 勝率", f"{100-prob_h:.1f}%")
                    
                    show_odds = st.toggle("顯示盤口分析與讓分", key=f"tog_{g_key}")
                    if show_odds:
                        st.write(f"📊 預估讓分: **{h_cn} {pred_spread:+.1f}**")
                        c1, c2 = st.columns(2)
                        oh = c1.number_input(f"[主] 賠率", value=1.85, key=f"h_{g_key}")
                        oa = c2.number_input(f"[客] 賠率", value=1.85, key=f"a_{g_key}")
                        edge = (prob_h - (1/oh*100)) if prob_h > 50 else ((100-prob_h) - (1/oa*100))
                        st.info(f"💡 價值優勢: {edge:+.1f}%")
                    
                    results.append({'label': f"[客] {a_cn} vs [主] {h_cn}", 'h_res': h_res, 'a_res': a_res, 'h_cn': h_cn, 'a_cn': a_cn})

        if results:
            st.divider()
            sel = st.selectbox("🔍 選擇對戰查看核心輪替與傷情", [x['label'] for x in results], key=f"sel_detail_{i}")
            curr = next(x for x in results if x['label'] == sel)
            
            st.markdown("#### 🚑 即時傷病詳情 (已修正翻譯與位置)")
            ic1, ic2 = st.columns(2)
            with ic1:
                st.write(f"**[主] {curr['h_cn']}**")
                if not curr['h_res']['inj_df'].empty:
                    st.table(curr['h_res']['inj_df'][['球員', '位置', '狀態', '說明']])
                else: st.success("✅ 目前無傷病紀錄")
            with ic2:
                st.write(f"**[客] {curr['a_cn']}**")
                if not curr['a_res']['inj_df'].empty:
                    st.table(curr['a_res']['inj_df'][['球員', '位置', '狀態', '說明']])
                else: st.success("✅ 目前無傷病紀錄")
