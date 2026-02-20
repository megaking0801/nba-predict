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
ID_MAP = {t['id']: t['abbreviation'] for t in teams.get_teams()}

# --- 2. 數據抓取引擎 ---
def fetch_safe_df(endpoint, **kwargs):
    try:
        r = endpoint(**kwargs).get_dict()
        res = r['resultSets'][0]
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

@st.cache_data(ttl=3600)
def get_global_data():
    # 1. 抓取球員數據
    ps = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season='2025-26', per_mode_detailed='PerGame')
    if not ps.empty and 'TEAM_ID' in ps.columns:
        ps['IMPACT'] = ps['PTS'] + ps['REB']*1.1 + ps['AST']*1.5 + (ps['STL']+ps['BLK'])*2 - ps['TOV']*2
        ps['NORM'] = ps['PLAYER_NAME'].astype(str).str.lower().str.strip()
    else:
        ps = pd.DataFrame(columns=['PLAYER_NAME', 'TEAM_ID', 'PTS', 'IMPACT', 'NORM'])

    # 2. 抓取所有隊伍 Context (B2B)
    ctx = {}
    now_us = datetime.now(us_east_tz)
    yesterday_us = (now_us - timedelta(days=1)).strftime('%Y-%m-%d')
    for tid in VALID_TEAM_IDS:
        log = fetch_safe_df(teamgamelog.TeamGameLog, team_id=tid, season='2025-26')
        is_b2b, recent_w = False, 0.5
        if not log.empty:
            log['GAME_DATE'] = pd.to_datetime(log['GAME_DATE'])
            is_b2b = any(log['GAME_DATE'].dt.strftime('%Y-%m-%d') == yesterday_us)
            recent_w = (log.head(5)['WL'] == 'W').mean()
        ctx[tid] = {'b2b': is_b2b, 'recent_w': recent_w}

    # 3. [修正重點] 強化傷病報告解析
    inj_list = []
    try:
        url = "https://www.espn.com/nba/injuries"
        headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 定義缺陣與觀察的關鍵字庫
        OUT_KEYS = ['out', 'surgery', 'suspended', '报销', 'season', 'left knee', 'right knee'] # 增加常見缺陣描述
        QUES_KEYS = ['questionable', 'gtd', 'day-to-day', 'doubtful', 'observation']

        for table in soup.select('.ResponsiveTable'):
            t_name_raw = table.select_one('.Table__Title').get_text(strip=True)
            t_abbr = next((a for a, info in TEAM_MAP.items() if any(n.lower() in t_name_raw.lower() for n in info)), "UNK")
            
            for r in table.select('tbody tr'):
                cols = r.select('td')
                if len(cols) >= 3:
                    p_name = re.sub(r'(PG|SG|SF|PF|C|G|F)$', '', cols[0].get_text(strip=True))
                    raw_st = cols[2].get_text(strip=True).lower() # 狀態欄
                    raw_re = cols[3].get_text(strip=True).lower() if len(cols)>3 else "" # 原因欄
                    
                    full_text = raw_st + " " + raw_re
                    
                    # 邏輯判斷：原因寫 Out 或狀態寫 Out 都算缺陣
                    is_out = any(w in full_text for w in OUT_KEYS)
                    is_ques = any(w in full_text for w in QUES_KEYS)
                    
                    if is_out:
                        status_cn = "❌ [確定缺陣]"
                    elif is_ques:
                        status_cn = "📋 [觀察名單]"
                    else:
                        status_cn = "✅ [預計出賽]"
                        
                    inj_list.append({
                        'NORM': p_name.lower().strip(), 
                        '球員': p_name, 
                        '狀態': status_cn, 
                        '原因': raw_re.upper() if raw_re else "無", 
                        '球隊': t_abbr, 
                        'IS_OUT': is_out
                    })
    except: pass
    return ps, ctx, pd.DataFrame(inj_list)

# --- 3. UI 初始化 ---
st.set_page_config(page_title="NBA Edge v16.4", layout="wide")

h_top1, h_top2 = st.columns([0.8, 0.2])
with h_top1: 
    st.title("🏀 NBA Edge 數據預測系統")
    st.caption(f"台灣時間：{datetime.now(tw_tz).strftime('%m/%d %H:%M')}")
with h_top2:
    with st.popover("💡 挑場規則"):
        st.markdown("1. **Edge > 5.0**\\n2. 從中選 **過盤率** 前 3 高\\n3. 不達標不硬湊")

with st.spinner("⚡ 數據同步中..."):
    ps_db, ctx_db, inj_db = get_global_data()

now_us = datetime.now(us_east_tz)
target_date_us = now_us.strftime('%m/%d/%Y')
sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=target_date_us)

if sb.empty or len(sb[sb['HOME_TEAM_ID'].isin(VALID_TEAM_IDS)]) == 0:
    target_date_us = (now_us + timedelta(days=1)).strftime('%m/%d/%Y')
    sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=target_date_us)
    st.info(f"📅 顯示美東明日賽程：{target_date_us}")
else:
    st.success(f"📅 分析美東今日賽程：{target_date_us}")

if not sb.empty:
    all_games_data = []
    sb_f = sb[sb['HOME_TEAM_ID'].isin(VALID_TEAM_IDS)]
    for _, row in sb_f.iterrows():
        h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
        h_abbr, a_abbr = ID_MAP.get(h_id), ID_MAP.get(a_id)
        
        def build_pkg(tid, abbr):
            ctx = ctx_db.get(tid, {'b2b': False, 'recent_w': 0.5})
            t_inj = inj_db[inj_db['球隊'] == abbr] if not inj_db.empty else pd.DataFrame()
            out_list = t_inj[t_inj['IS_OUT']]['NORM'].tolist()
            active = ps_db[(ps_db['TEAM_ID'] == tid) & (~ps_db['NORM'].isin(out_list))].sort_values('IMPACT', ascending=False) if 'TEAM_ID' in ps_db.columns else pd.DataFrame()
            return {'pts': active['PTS'].sum(), 'impact': active['IMPACT'].mean(), 'df': active, 'inj': t_inj, 'b2b': ctx['b2b'], 'recent_w': ctx['recent_w']}
        
        h_p, a_p = build_pkg(h_id, h_abbr), build_pkg(a_id, a_abbr)
        b2b_v = (-2.5 if h_p['b2b'] else 0) - (-2.5 if a_p['b2b'] else 0)
        recent_v = (h_p['recent_w'] - a_p['recent_w']) * 5
        base_diff = (h_p['pts'] - a_p['pts']) * 0.09 + (h_p['impact'] - a_p['impact']) * 3.8 + 2.5 + b2b_v + recent_v
        raw_prob = 1 / (1 + 10**(-abs(base_diff)/11)) * 100
        
        all_games_data.append({'label': f"{TEAM_NAME_CH.get(a_abbr)}(客) @ {TEAM_NAME_CH.get(h_abbr)}(主)", 'base_diff': base_diff, 'raw_prob': raw_prob, 'h_pkg': h_p, 'a_pkg': a_p, 'h_cn': TEAM_NAME_CH.get(h_abbr), 'a_cn': TEAM_NAME_CH.get(a_abbr)})

    # --- 區域一：專業選場 (Edge > 5.0) ---
    st.header("🎯 專業串關挑場 (Edge > 5.0)")
    qualified = [g for g in all_games_data if abs(g['base_diff']) > 5.0]
    final_picks = sorted(qualified, key=lambda x: x['raw_prob'], reverse=True)[:3]
    
    if not final_picks:
        st.warning("⚠️ 今日 Edge 皆未達 5.0，建議觀望。")
    else:
        t_cols = st.columns(len(final_picks))
        for idx, g in enumerate(final_picks):
            with t_cols[idx]:
                with st.container(border=True):
                    rec_side = g['h_cn'] if g['base_diff'] > 0 else g['a_cn']
                    st.subheader(f"精選 Pick {idx+1}")
                    st.write(f"**{g['label']}**")
                    st.metric("基礎 Edge", f"{abs(g['base_diff']):.1f}")
                    st.write(f"預估過盤率: **{g['raw_prob']:.1f}%**")
                    st.success(f"推薦：{rec_side}")

    st.divider()

    # --- 區域二：全部場次 ---
    st.header("🎯 全部場次與實時預測")
    for i in range(0, len(all_games_data), 3):
        cols = st.columns(3)
        for j, g in enumerate(all_games_data[i:i+3]):
            with cols[j]:
                with st.container(border=True):
                    st.subheader(g['label'])
                    u_sp = st.number_input("讓分值 (主讓負數)", value=0.0, step=0.5, key=f"sp_{g['label']}")
                    u_oh = st.number_input("主賠", 1.01, 5.0, 1.90, key=f"oh_{g['label']}")
                    u_oa = st.number_input("客賠", 1.01, 5.0, 1.90, key=f"oa_{g['label']}")
                    f_edge = g['base_diff'] + u_sp
                    prob = 1 / (1 + 10**(-abs(f_edge)/11)) * 100
                    rec = g['h_cn'] if f_edge > 0 else g['a_cn']
                    odds = u_oh if f_edge > 0 else u_oa
                    ev = (prob/100 * odds) - 1
                    st.write(f"勝率: **{prob:.1f}%** | Edge: **{abs(f_edge):.1f}**")
                    st.write(f"EV: **{ev*100:+.1f}%**")
                    if ev > 0.05: st.success(f"🔥 推薦：{rec}")
                    else: st.info(f"建議：{rec}")

    # --- 區域三：深度查詢 ---
    st.divider()
    st.header("🔍 深度數據分析")
    sel = st.selectbox("選擇場次細看數據", [g['label'] for g in all_games_data])
    if sel:
        curr = next(g for g in all_games_data if g['label'] == sel)
        st.write(f"📊 **戰前速報**：{'🚨 客隊 B2B' if curr['a_pkg']['b2b'] else '✅ 客隊體能正常'} | {'🚨 主隊 B2B' if curr['h_pkg']['b2b'] else '✅ 主隊體能正常'}")
        c_h, c_a = st.columns(2)
        for col, pkg, side in zip([c_h, c_a], [curr['h_pkg'], curr['a_pkg']], ["(主)", "(客)"]):
            with col:
                st.subheader(f"{curr['h_cn' if side=='(主)' else 'a_cn']} {side}")
                st.write(f"近五場勝率: **{pkg['recent_w']*100:.0f}%**")
                st.dataframe(pkg['df'][['PLAYER_NAME', 'PTS', 'IMPACT']].head(12), hide_index=True)
                if not pkg['inj'].empty: 
                    st.write("**🚑 傷病名單**")
                    st.dataframe(pkg['inj'][['球員', '狀態', '原因']], hide_index=True)
                else: st.write("✅ 目前無傷病報告")
