import streamlit as st
from nba_api.stats.endpoints import scoreboardv2, leaguedashplayerstats, teamgamelog
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
VALID_TEAM_IDS = [t['id'] for t in teams.get_teams()]

# --- 2. 核心抓取工具 ---
def fetch_safe_df(endpoint, **kwargs):
    try:
        r = endpoint(**kwargs).get_dict()
        res = r['resultSets'][0]
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

@st.cache_data(ttl=3600)
def get_all_teams_context():
    """預先抓取所有球隊的狀態，避免切換選單時卡頓"""
    yesterday = (datetime.now(us_east_tz) - timedelta(days=1)).strftime('%Y-%m-%d')
    context_map = {}
    for team_id in VALID_TEAM_IDS:
        log = fetch_safe_df(teamgamelog.TeamGameLog, team_id=team_id, season='2025-26')
        is_b2b, recent_w = False, 0.5
        if not log.empty:
            log['GAME_DATE'] = pd.to_datetime(log['GAME_DATE'])
            is_b2b = any(log['GAME_DATE'].dt.strftime('%Y-%m-%d') == yesterday)
            recent_w = (log.head(5)['WL'] == 'W').mean()
        context_map[team_id] = {'b2b': is_b2b, 'recent_w': recent_w}
    return context_map

def translate_status(status_text, reason_text):
    full = f"{status_text} {reason_text}".lower()
    if any(w in full for w in ['out', 'surgery', 'suspended', '报销', 'season']): return "❌ [確定缺陣]"
    if any(w in full for w in ['questionable', 'gtd', 'day-to-day', 'doubtful']): return "📋 [觀察名單]"
    return "✅ [預計出賽]"

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
                    st_cn = translate_status(cols[2].get_text(strip=True), cols[3].get_text(strip=True) if len(cols)>3 else "")
                    all_inj.append({'NORM': p_name.lower().strip(), '球員': p_name, '狀態': st_cn, '原因': cols[3].get_text(strip=True) if len(cols)>3 else "無", '球隊': t_abbr, 'IS_OUT': "❌" in st_cn})
    except: pass
    return pd.DataFrame(all_inj)

@st.cache_data(ttl=3600)
def load_nba_stats():
    df = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season='2025-26', per_mode_detailed='PerGame')
    if df.empty: return pd.DataFrame()
    df['IMPACT'] = df['PTS'] + df['REB']*1.1 + df['AST']*1.5 + (df['STL']+df['BLK'])*2 - df['TOV']*2
    df['NORM'] = df['PLAYER_NAME'].astype(str).str.lower().str.strip()
    return df

# --- 3. UI 初始化 ---
st.set_page_config(page_title="NBA Edge v15.7", layout="wide")

# [置頂] 右上角 Hint 說明
h1, h2 = st.columns([0.8, 0.2])
with h1:
    st.title("🏀 NBA Edge 數據預測系統")
with h2:
    pop = st.popover("💡 判讀指南")
    pop.markdown("""
    **Edge (優勢分)**: 模型預測分差與盤口的差距。
    **EV (期望值)**: 高於 10% 為極佳機會。
    """)

# 預載入數據
with st.status("正在抓取 NBA 即時數據...", expanded=False) as status:
    ps_db = load_nba_stats()
    inj_db = get_espn_injuries()
    ctx_db = get_all_teams_context() # 關鍵優化：一次性抓完所有 B2B
    status.update(label="數據載入完成！", state="complete")

nba_today = datetime.now(us_east_tz)
sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=nba_today.strftime('%m/%d/%Y'))
if sb.empty: sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=(nba_today + timedelta(days=1)).strftime('%m/%d/%Y'))

id_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}

if not sb.empty:
    all_games_data = []
    for _, row in sb[sb['HOME_TEAM_ID'].isin(VALID_TEAM_IDS)].iterrows():
        h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
        h_abbr, a_abbr = id_map.get(h_id), id_map.get(a_id)
        
        def get_pkg(tid, abbr):
            ctx = ctx_db.get(tid, {'b2b': False, 'recent_w': 0.5})
            t_inj = inj_db[inj_db['球隊'] == abbr] if not inj_db.empty else pd.DataFrame()
            out_list = t_inj[t_inj['IS_OUT']]['NORM'].tolist() if not t_inj.empty else []
            active = ps_db[(ps_db['TEAM_ID'] == tid) & (~ps_db['NORM'].isin(out_list))].sort_values('IMPACT', ascending=False)
            return {'pts': active['PTS'].sum(), 'impact': active['IMPACT'].mean(), 'df': active, 'inj': t_inj, 'b2b': ctx['b2b'], 'recent_w': ctx['recent_w']}

        h_pkg, a_pkg = get_pkg(h_id, h_abbr), get_pkg(a_id, a_abbr)
        
        # 預計算基礎分差 (不再重抓 API)
        b2b_diff = (-2.5 if h_pkg['b2b'] else 0) - (-2.5 if a_pkg['b2b'] else 0)
        recent_diff = (h_pkg['recent_w'] - a_pkg['recent_w']) * 5
        base_diff = (h_pkg['pts'] - a_pkg['pts']) * 0.09 + (h_pkg['impact'] - a_pkg['impact']) * 3.8 + 2.5 + b2b_diff + recent_diff
        
        all_games_data.append({
            'label': f"{TEAM_NAME_CH.get(a_abbr)}(客) @ {TEAM_NAME_CH.get(h_abbr)}(主)",
            'h_cn': TEAM_NAME_CH.get(h_abbr), 'a_cn': TEAM_NAME_CH.get(a_abbr),
            'base_diff': base_diff, 'h_pkg': h_pkg, 'a_pkg': a_pkg
        })

    # --- 區域一：即時推薦 ---
    st.header("🎯 今日對戰組合與實時預測")
    for i in range(0, len(all_games_data), 3):
        cols = st.columns(3)
        for j, g in enumerate(all_games_data[i:i+3]):
            with cols[j]:
                with st.container(border=True):
                    st.subheader(g['label'])
                    u_sp = st.number_input("受讓分(主+客-)", 0.0, step=0.5, key=f"sp_{g['label']}")
                    u_oh = st.number_input("主賠", 1.01, 5.0, 1.90, key=f"oh_{g['label']}")
                    u_oa = st.number_input("客賠", 1.01, 5.0, 1.90, key=f"oa_{g['label']}")
                    
                    # 邏輯即時計算
                    final_edge = g['base_diff'] + u_sp
                    win_prob = 1 / (1 + 10**(-abs(final_edge)/11)) * 100
                    rec = g['h_cn'] if final_edge > 0 else g['a_cn']
                    odds = u_oh if final_edge > 0 else u_oa
                    ev = (win_prob/100 * odds) - 1
                    
                    st.write(f"勝率: **{win_prob:.1f}%** | Edge: **{abs(final_edge):.1f}**")
                    st.write(f"EV: **{ev*100:+.1f}%**")
                    if ev > 0.05: st.success(f"🔥 推薦：{rec}")
                    else: st.info(f"建議：{rec}")

    # --- 區域二：深度查詢 (效能優化版) ---
    st.divider()
    st.header("🔍 深度數據查詢")
    sel = st.selectbox("請選擇場次", [g['label'] for g in all_games_data])
    if sel:
        # 這裡現在是毫秒級反應，因為資料全在記憶體裡了
        curr = next(g for g in all_games_data if g['label'] == sel)
        st.write(f"📊 **戰前速報**：{'🚨 客隊背靠背' if curr['a_pkg']['b2b'] else '✅ 客隊體能正常'} | {'🚨 主隊背靠背' if curr['h_pkg']['b2b'] else '✅ 主隊體能正常'}")
        
        c_h, c_a = st.columns(2)
        for col, pkg, side in zip([c_h, c_a], [curr['h_pkg'], curr['a_pkg']], ["(主)", "(客)"]):
            with col:
                st.subheader(f"{curr['h_cn' if side=='(主)' else 'a_cn']} {side}")
                st.write(f"近五場勝率: **{pkg['recent_w']*100:.0f}%**")
                st.dataframe(pkg['df'][['PLAYER_NAME', 'PTS', 'IMPACT']].head(12), hide_index=True)
                st.write("**🚑 傷病名單**")
                if not pkg['inj'].empty: st.dataframe(pkg['inj'][['球員', '狀態', '原因']], hide_index=True)
                else: st.write("✅ 目前無傷病報告")
