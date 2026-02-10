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

# 擴展球隊字典，增加全名比對以提升抓取成功率
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
    'Achilles': '阿基里斯腱', 'Calf': '小腿', 'Knee': '膝蓋', 'Ankle': '腳踝', 'Foot': '腳部',
    'Hamstring': '大腿後肌', 'Back': '背部', 'Shoulder': '肩膀', 'Wrist': '手腕', 'Thumb': '拇指',
    'Groin': '鼠蹊部', 'Hip': '臀部', 'Hand': '手部', 'Neck': '頸部', 'Elbow': '手肘',
    'Surgery': '手術', 'Rest': '輪休', 'sprain': '扭傷', 'strain': '拉傷',
    'soreness': '痠痛', 'fracture': '骨折', 'torn': '撕裂'
}

def clean_description(text):
    """擷取傷情重點，縮短說明"""
    if not text: return "未知"
    # 移除常見的長難句贅詞
    text = re.sub(r'(is|was|has been) (expected to|slated to|ruled|out for).*?(the|until)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'remains out for .*? against .*?\.', '', text, flags=re.IGNORECASE)
    # 只取前 25 個字或第一個句號前的內容
    text = text.split('.')[0]
    return translate_text(text)

def translate_text(text):
    if not text or pd.isna(text): return ""
    res = str(text)
    for eng, chi in TRANS_DICT.items():
        res = re.sub(eng, chi, res, flags=re.IGNORECASE)
    return res

st.set_page_config(page_title="NBA 數據專家 v13.5", layout="wide")

# --- 2. 傷病解析引擎 (v13.5 強度穩定版) ---
@st.cache_data(ttl=600)
def get_espn_injuries_v2():
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'}
    all_inj = []
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        # 抓取所有球隊區塊
        tables = soup.select('.ResponsiveTable')
        
        for table in tables:
            title_node = table.select_one('.Table__Title')
            if not title_node: continue
            
            t_name_text = title_node.get_text(strip=True)
            t_abbr = "UNKNOWN"
            # 改良的比對邏輯：比對全名或簡寫
            for abbr, info in TEAM_MAP.items():
                if any(name.lower() in t_name_text.lower() for name in info):
                    t_abbr = abbr
                    break
            
            rows = table.select('tbody tr')
            for r in rows:
                cols = r.select('td')
                if len(cols) >= 3:
                    p_name = re.sub(r'(PG|SG|SF|PF|C|G|F)$', '', cols[0].get_text(strip=True))
                    status_raw = cols[1].get_text(strip=True)
                    desc_raw = cols[2].get_text(strip=True)

                    all_inj.append({
                        '球員': p_name,
                        '狀態': translate_text(status_raw),
                        '說明': clean_description(desc_raw),
                        '球隊': t_abbr,
                        'RAW_STATUS': status_raw
                    })
    except Exception as e:
        st.error(f"傷病抓取異常: {e}")
    return pd.DataFrame(all_inj)

@st.cache_data(ttl=3600)
def load_all_nba_stats():
    S = '2025-26'
    p_base = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    p_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    if p_base.empty: return pd.DataFrame(), {}, "N/A"
    p_full = pd.merge(p_base, p_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID', how='left')
    # 計算影響力指標
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
injury_df = get_espn_injuries_v2()

# --- 3. UI 顯示邏輯 ---
st.title("🏀 NBA 數據專家 v13.5 (強化分析版)")
st.sidebar.write(f"📊 數據同步時間: {update_time}")

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
                # 過濾確定不上的球員 (Out, Doubtful)
                out_names = t_inj[t_inj['RAW_STATUS'].str.contains('Out|Doubtful', case=False, na=False)]['球員'].tolist()
                all_ps = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
                # 排除傷病球員，計算當前「實際上場核心」數據
                active_core = all_ps[~all_ps['PLAYER_NAME'].apply(lambda x: any(name in x for name in out_names))].head(8)
                return {'pts': active_core['PTS'].sum(), 'ts': active_core['TS_PCT'].mean(), 'pie': active_core['PIE'].mean(), 'df': active_core, 'inj_df': t_inj}

            h_res, a_res = build_team_package(h_id, h_abbr), build_team_package(a_id, a_abbr)
            
            # 勝率演算法 (綜合核心球員體系 + 近期走勢)
            final_margin = (h_res['pts']-a_res['pts'])*0.12 + (h_res['ts']-a_res['ts'])*15 + (h_res['pie']-a_res['pie'])*45 + (l10_db.get(h_id,0)-l10_db.get(a_id,0))*0.4 + 2.8
            prob_h = 1 / (1 + 10**(-final_margin/15)) * 100
            
            h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
            g_key = f"v135_{dates[i].strftime('%Y%m%d')}_{a_abbr}_{h_abbr}"
            
            with cols[idx % 3]:
                with st.container(border=True):
                    st.markdown(f"#### {a_cn} @ {h_cn}")
                    st.progress(prob_h/100)
                    st.write(f"🏠 {h_cn} 勝率: **{prob_h:.1f}%**")
                    st.write(f"🚌 {a_cn} 勝率: **{100-prob_h:.1f}%**")
                    
                    show_odds = st.toggle("分析價值", key=f"tog_{g_key}")
                    if show_odds:
                        c1, c2 = st.columns(2)
                        oh = c1.number_input(f"主賠", value=1.85, key=f"h_{g_key}")
                        oa = c2.number_input(f"客賠", value=1.85, key=f"a_{g_key}")
                        edge = (prob_h - (1/oh*100)) if prob_h > 50 else ((100-prob_h) - (1/oa*100))
                        st.caption(f"價值優勢: {edge:+.1f}%")
                    
                    results.append({'label': f"🏀 {a_cn} vs {h_cn}", 'h_res': h_res, 'a_res': a_res, 'h_cn': h_cn, 'a_cn': a_cn})

        if results:
            st.divider()
            sel = st.selectbox("🔍 深度查看對戰球員與傷情", [x['label'] for x in results], key=f"sel_detail_{i}")
            curr = next(x for x in results if x['label'] == sel)
            
            col_a, col_b = st.columns(2)
            with col_a:
                st.subheader(f"🛡️ {curr['h_cn']} 核心輪替")
                st.dataframe(curr['h_res']['df'][['PLAYER_NAME', 'PTS', 'REB', 'AST', 'PIE']], use_container_width=True)
                st.write("**🚑 傷病報告**")
                if not curr['h_res']['inj_df'].empty:
                    st.table(curr['h_res']['inj_df'][['球員', '狀態', '說明']])
                else: st.success("健康狀態良好")
                
            with col_b:
                st.subheader(f"🛡️ {curr['a_cn']} 核心輪替")
                st.dataframe(curr['a_res']['df'][['PLAYER_NAME', 'PTS', 'REB', 'AST', 'PIE']], use_container_width=True)
                st.write("**🚑 傷病報告**")
                if not curr['a_res']['inj_df'].empty:
                    st.table(curr['a_res']['inj_df'][['球員', '狀態', '說明']])
                else: st.success("健康狀態良好")
