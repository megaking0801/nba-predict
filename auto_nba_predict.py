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

TRANS_DICT = {
    r'\bOut\b': '❌ 缺陣',
    r'\bDay-To-Day\b': '📋 每日觀察',
    r'\bGTD\b': '📋 賽前決定',
    r'\bQuestionable\b': '🤔 出戰成疑',
    r'\bDoubtful\b': '😰 極大機率缺陣',
    r'\bProbable\b': '✅ 可能出戰',
    r'\bG\b': '後衛', r'\bF\b': '前鋒', r'\bC\b': '中鋒',
    r'\bPG\b': '控衛', r'\bSG\b': '得分衛', r'\bSF\b': '小前鋒', r'\bPF\b': '大前鋒',
    'Achilles': '阿基里斯腱', 'Knee': '膝蓋', 'Ankle': '腳踝', 'Foot': '腳部',
    'Back': '背部', 'Shoulder': '肩膀', 'Wrist': '手腕', 'Surgery': '手術',
    'sprain': '扭傷', 'strain': '拉傷', 'soreness': '痠痛', 'Concussion': '腦震盪'
}

# --- 工具函數 ---
def normalize_name(name):
    if not isinstance(name, str): return ""
    name = ''.join(c for c in unicodedata.normalize('NFD', name) if unicodedata.category(c) != 'Mn')
    name = name.lower()
    name = re.sub(r'\b(jr\.?|sr\.?|ii|iii|iv)\b', '', name)
    name = re.sub(r'[.\']', '', name)
    nicknames = {'nic ': 'nicolas ', 'cam ': 'cameron ', 'chris ': 'christopher ', 'pjwashington': 'pj washington'}
    for nick, full in nicknames.items():
        if name.startswith(nick): name = name.replace(nick, full)
    return name.strip()

def clean_description(text):
    if not text or pd.isna(text): return "尚無細節"
    text = re.sub(r'(is|was|has been) (expected to|slated to|ruled|out for).*?(the|until)', '', str(text), flags=re.IGNORECASE)
    summary = str(text).split('.')[0]
    return translate_text(summary)

def translate_text(text):
    if not text or pd.isna(text): return ""
    res = str(text)
    for eng, chi in TRANS_DICT.items():
        res = re.sub(eng, chi, res, flags=re.IGNORECASE)
    return res

st.set_page_config(page_title="NBA 數據專家 v13.10", layout="wide")

# --- 2. 數據抓取引擎 ---
@st.cache_data(ttl=600)
def get_espn_injuries_v7():
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0'}
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
                    p_name = re.sub(r'(PG|SG|SF|PF|C|G|F)$', '', cols[0].get_text(strip=True))
                    pos_raw = cols[1].get_text(strip=True)
                    col2_text = cols[2].get_text(strip=True)
                    col3_text = cols[3].get_text(strip=True) if len(cols) > 3 else ""
                    
                    # 全文掃描判定缺陣
                    full_text_check = (col2_text + " " + col3_text).lower()
                    is_out = 'out' in full_text_check or 'doubtful' in full_text_check or 'injured' in full_text_check
                    
                    all_inj.append({
                        '球員': p_name,
                        'NORMALIZED_NAME': normalize_name(p_name),
                        '位置': translate_text(pos_raw),
                        '狀態': translate_text(col2_text),
                        '說明': clean_description(col3_text),
                        '球隊': t_abbr,
                        'IS_OUT': is_out
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
    p_full['NORMALIZED_NAME'] = p_full['PLAYER_NAME'].apply(normalize_name)
    
    gf = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    l10 = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(10, min_periods=3).mean()).groupby(gf['TEAM_ID']).last().to_dict() if not gf.empty else {}
    return p_full, l10, datetime.now(tw_tz).strftime("%H:%M")

def fetch_safe_df(endpoint, **kwargs):
    try:
        r = endpoint(**kwargs).get_dict()
        res = r['resultSets'][0] if 'resultSets' in r else r['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

# --- 數據準備 ---
ps_db, l10_db, update_time = load_all_nba_stats()
injury_df = get_espn_injuries_v7()

# --- 3. UI 顯示 ---
st.title("🏀 NBA 數據專家 v13.10 (賠率連動版)")
st.sidebar.write(f"📊 數據最後更新: {update_time}")

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
                out_players_norm = t_inj[t_inj['IS_OUT'] == True]['NORMALIZED_NAME'].tolist()
                
                all_ps = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
                active_core = all_ps[~all_ps['NORMALIZED_NAME'].isin(out_players_norm)].head(8)
                excluded = all_ps[all_ps['NORMALIZED_NAME'].isin(out_players_norm)]['PLAYER_NAME'].tolist()
                
                return {
                    'pts': active_core['PTS'].sum(), 'ts': active_core['TS_PCT'].mean(), 'pie': active_core['PIE'].mean(), 
                    'df': active_core, 'inj_df': t_inj, 'excluded_names': excluded
                }

            h_res, a_res = build_team_package(h_id, h_abbr), build_team_package(a_id, a_abbr)
            
            # --- 1. 純數據模型勝率 (Base Model) ---
            diff = (h_res['pts']-a_res['pts'])*0.12 + (h_res['ts']-a_res['ts'])*15 + (h_res['pie']-a_res['pie'])*45 + (l10_db.get(h_id,0)-l10_db.get(a_id,0))*0.4 + 2.5
            model_prob_h = 1 / (1 + 10**(-diff/15)) * 100
            
            h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
            g_key = f"v1310_{dates[i].strftime('%Y%m%d')}_{a_abbr}_{h_abbr}"
            
            with cols[idx % 3]:
                with st.container(border=True):
                    st.markdown(f"### [客] {a_cn} vs [主] {h_cn}")
                    
                    # 預設顯示模型勝率
                    final_prob_h = model_prob_h
                    final_spread = -diff
                    
                    # --- 賠率輸入區 (移到上方，直接影響結果) ---
                    use_odds = st.checkbox("輸入賠率修正勝率", key=f"chk_{g_key}")
                    
                    if use_odds:
                        c_h, c_a = st.columns(2)
                        oh = c_h.number_input("主賠", value=1.90, step=0.01, key=f"h_{g_key}")
                        oa = c_a.number_input("客賠", value=1.90, step=0.01, key=f"a_{g_key}")
                        
                        # --- 2. 莊家隱含勝率 ---
                        # 移除抽水 (Vig) 後的真實機率
                        imp_h = 1/oh
                        imp_a = 1/oa
                        total_imp = imp_h + imp_a
                        real_prob_h_odds = (imp_h / total_imp) * 100
                        
                        # --- 3. 融合計算 (60% 模型 + 40% 莊家) ---
                        final_prob_h = (model_prob_h * 0.6) + (real_prob_h_odds * 0.4)
                        
                        # 反推修正後的讓分
                        # 簡單公式：每 3% 勝率差約等於 1 分差距
                        spread_adj = (final_prob_h - 50) / 3.0 * 2 # 係數調整
                        final_spread = spread_adj 

                        st.caption(f"📉 純數據勝率: {model_prob_h:.1f}% | 🏦 莊家暗示: {real_prob_h_odds:.1f}%")

                    # --- 顯示最終結果 (會隨賠率變動) ---
                    st.divider()
                    st.metric(f"🏆 {h_cn} 最終勝率", f"{final_prob_h:.1f}%", delta=f"{final_prob_h-model_prob_h:.1f}%" if use_odds else None)
                    st.metric(f"🛡️ {h_cn} 預估讓分", f"{final_spread:+.1f}")
                    st.metric(f"⚔️ {a_cn} 最終勝率", f"{100-final_prob_h:.1f}%")

                    results.append({'label': f"[客] {a_cn} vs [主] {h_cn}", 'h_res': h_res, 'a_res': a_res, 'h_cn': h_cn, 'a_cn': a_cn})

        if results:
            st.divider()
            sel = st.selectbox("🔍 查看詳細數據", [x['label'] for x in results], key=f"sel_detail_{i}")
            curr = next(x for x in results if x['label'] == sel)
            
            st.markdown("#### 🚑 傷病報告 (已嚴格過濾)")
            ic1, ic2 = st.columns(2)
            with ic1:
                st.write(f"**[主] {curr['h_cn']}**")
                if not curr['h_res']['inj_df'].empty: st.dataframe(curr['h_res']['inj_df'][['球員', '位置', '狀態', 'IS_OUT']], hide_index=True)
                else: st.success("✅ 全員健康")
            with ic2:
                st.write(f"**[客] {curr['a_cn']}**")
                if not curr['a_res']['inj_df'].empty: st.dataframe(curr['a_res']['inj_df'][['球員', '位置', '狀態', 'IS_OUT']], hide_index=True)
                else: st.success("✅ 全員健康")

            st.markdown("#### 🛡️ 核心 8 人數據")
            pc1, pc2 = st.columns(2)
            with pc1:
                if curr['h_res']['excluded_names']: st.error(f"🚫 已排除: {', '.join(curr['h_res']['excluded_names'])}")
                st.dataframe(curr['h_res']['df'][['PLAYER_NAME', 'PTS', 'IMPACT', 'PIE']].style.format(precision=1), use_container_width=True)
            with pc2:
                if curr['a_res']['excluded_names']: st.error(f"🚫 已排除: {', '.join(curr['a_res']['excluded_names'])}")
                st.dataframe(curr['a_res']['df'][['PLAYER_NAME', 'PTS', 'IMPACT', 'PIE']].style.format(precision=1), use_container_width=True)
