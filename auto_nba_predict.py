import streamlit as st
from nba_api.stats.endpoints import scoreboardv2, leaguedashplayerstats
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
def translate_reason(text):
    if not text or text == "無": return "正常"
    trans = {
        'Knee': '膝蓋', 'Ankle': '腳踝', 'Foot': '腳部', 'Hamstring': '大腿後側',
        'Back': '背部', 'Shoulder': '肩膀', 'Wrist': '手腕', 'Thumb': '拇指',
        'Illness': '疾病', 'Rest': '輪休', 'Soreness': '痠痛', 'Strain': '拉傷',
        'Sprain': '扭傷', 'Surgery': '手術', 'Conditioning': '身體狀態', 'Out': '缺陣'
    }
    for eng, chi in trans.items():
        text = re.sub(eng, chi, text, flags=re.IGNORECASE)
    return text

def translate_status(status_text, reason_text):
    full = f"{status_text} {reason_text}".lower()
    if any(w in full for w in ['out', 'surgery', 'suspended', '报销']): return "❌ [確定缺陣]"
    if any(w in full for w in ['questionable', 'gtd', 'day-to-day', 'doubtful']): return "📋 [觀察名單]"
    return "✅ [預計出賽]"

def fetch_safe_df(endpoint, **kwargs):
    try:
        r = endpoint(**kwargs).get_dict()
        res = r['resultSets'][0]
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

# --- 2. 數據引擎 (修復 AttributeError) ---
@st.cache_data(ttl=600)
def get_espn_injuries():
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0'}
    all_inj = []
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for table in soup.select('.ResponsiveTable'):
            t_name = table.select_one('.Table__Title').get_text(strip=True)
            t_abbr = next((a for a, info in TEAM_MAP.items() if any(n.lower() in t_name.lower() for n in info)), "UNK")
            for r in table.select('tbody tr'):
                cols = r.select('td')
                if len(cols) >= 3:
                    p_name = re.sub(r'(PG|SG|SF|PF|C|G|F)$', '', cols[0].get_text(strip=True))
                    raw_st = cols[2].get_text(strip=True)
                    raw_re = cols[3].get_text(strip=True) if len(cols)>3 else "無"
                    status = translate_status(raw_st, raw_re)
                    all_inj.append({
                        '球員': p_name, 'NORM': p_name.lower().strip(),
                        '狀態': status, '原因': translate_reason(raw_re),
                        '球隊': t_abbr, 'IS_OUT': "❌" in status
                    })
    except: pass
    return pd.DataFrame(all_inj) if all_inj else pd.DataFrame(columns=['球員','NORM','狀態','原因','球隊','IS_OUT'])

@st.cache_data(ttl=3600)
def load_nba_stats():
    # 獲取 2025-26 賽季數據
    p_full = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season='2025-26', per_mode_detailed='PerGame')
    
    # 安全檢查：如果數據為空，回傳帶有正確欄位的空 DataFrame
    if p_full.empty:
        st.error("⚠️ 無法獲取 NBA 球員數據，請檢查網路連線或稍後再試。")
        return pd.DataFrame(columns=['PLAYER_NAME', 'TEAM_ID', 'PTS', 'REB', 'AST', 'STL', 'BLK', 'TOV', 'IMPACT', 'NORM'])
    
    # 確保數據類型正確，避免 .str 操作失敗
    p_full['PLAYER_NAME'] = p_full['PLAYER_NAME'].astype(str)
    
    # 計算影響力指標
    p_full['IMPACT'] = p_full['PTS'] + p_full['REB']*1.1 + p_full['AST']*1.5 + (p_full['STL']+p_full['BLK'])*2 - p_full['TOV']*2
    p_full['NORM'] = p_full['PLAYER_NAME'].str.lower().strip()
    return p_full

# --- 3. UI 邏輯 ---
st.set_page_config(page_title="NBA Edge v15.0", layout="wide")
ps_db = load_nba_stats()
inj_db = get_espn_injuries()

nba_today = datetime.now(us_east_tz)
sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=nba_today.strftime('%m/%d/%Y'))
if sb.empty:
    sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=(nba_today + timedelta(days=1)).strftime('%m/%d/%Y'))

id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}

if not sb.empty:
    all_games = []
    sb_f = sb[sb['HOME_TEAM_ID'].isin(VALID_TEAM_IDS)]
    
    for idx, row in sb_f.iterrows():
        h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
        h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
        
        def process_team(tid, abbr):
            t_inj = inj_db[inj_db['球隊'] == abbr] if not inj_db.empty else pd.DataFrame(columns=['球員','NORM','狀態','原因','IS_OUT'])
            out_list = t_inj[t_inj['IS_OUT']]['NORM'].tolist()
            # 過濾掉確定不打的球員
            active = ps_db[(ps_db['TEAM_ID'] == tid) & (~ps_db['NORM'].isin(out_list))].sort_values('IMPACT', ascending=False)
            return {'pts': active['PTS'].sum(), 'impact': active['IMPACT'].mean(), 'df': active, 'inj': t_inj}

        h_pkg, a_pkg = process_team(h_id, h_abbr), process_team(a_id, a_abbr)
        h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
        
        all_games.append({
            'label': f"{a_cn}(客) @ {h_cn}(主)", 'h_cn': h_cn, 'a_cn': a_cn,
            'base_diff': (h_pkg['pts'] - a_pkg['pts']) * 0.12 + (h_pkg['impact'] - a_pkg['impact']) * 5 + 2.5,
            'h_pkg': h_pkg, 'a_pkg': a_pkg
        })

    # --- 區域一：即時賠率與預測 ---
    st.header("🎯 今日對戰組合與實時預測")
    for i in range(0, len(all_games), 3):
        cols = st.columns(3)
        for j, g in enumerate(all_games[i:i+3]):
            with cols[j]:
                with st.container(border=True):
                    st.subheader(g['label'])
                    u_sp = st.number_input("受讓分(主+客-)", 0.0, step=0.5, key=f"sp_{g['label']}")
                    u_oh = st.number_input("主賠", 1.01, 5.0, 1.90, key=f"oh_{g['label']}")
                    u_oa = st.number_input("客賠", 1.01, 5.0, 1.90, key=f"oa_{g['label']}")
                    
                    final_edge = g['base_diff'] + u_sp
                    win_prob = 1 / (1 + 10**(-abs(final_edge)/8)) * 100
                    rec = g['h_cn'] if final_edge > 0 else g['a_cn']
                    odds = u_oh if final_edge > 0 else u_oa
                    ev = (win_prob/100 * odds) - 1
                    
                    st.write(f"勝率: **{win_prob:.1f}%** | Edge: **{abs(final_edge):.1f}**")
                    st.write(f"EV: **{ev*100:+.1f}%**")
                    st.success(f"推薦：{rec}") if ev > 0.05 else st.info(f"建議：{rec}")

    # --- 區域二：深度數據查詢 ---
    st.divider()
    st.header("🔍 深度數據查詢")
    sel = st.selectbox("請選擇場次", [g['label'] for g in all_games])
    if sel:
        curr = next(g for g in all_games if g['label'] == sel)
        c_h, c_a = st.columns(2)
        for col, pkg, side in zip([c_h, c_a], [curr['h_pkg'], curr['a_pkg']], ["(主)", "(客)"]):
            with col:
                st.subheader(f"{curr['h_cn' if side=='(主)' else 'a_cn']} {side}")
                st.dataframe(pkg['df'][['PLAYER_NAME', 'PTS', 'IMPACT']].head(12), hide_index=True, use_container_width=True)
                st.write("**🚑 傷病與原因**")
                inj_display = pkg['inj'][['球員', '狀態', '原因']] if not pkg['inj'].empty else pd.DataFrame(columns=['球員','狀態','原因'])
                st.dataframe(inj_display, hide_index=True, use_container_width=True)
else:
    st.info("📅 目前無比賽進行中或尚未開賽。")
