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

TEAM_NAME_CH = {
    'ATL': '老鷹', 'BKN': '籃網', 'BOS': '塞爾提克', 'CHA': '黃蜂', 'CHI': '公牛', 'CLE': '騎士',
    'DAL': '獨行俠', 'DEN': '金塊', 'DET': '活塞', 'GSW': '勇士', 'HOU': '火箭', 'IND': '溜馬',
    'LAC': '快艇', 'LAL': '湖人', 'MEM': '灰熊', 'MIA': '熱火', 'MIL': '公鹿', 'MIN': '灰狼',
    'NOP': '鵜鶘', 'NYK': '尼克', 'OKC': '雷霆', 'ORL': '魔術', 'PHI': '76人', 'PHX': '太陽',
    'POR': '拓荒者', 'SAC': '國王', 'SAS': '馬刺', 'TOR': '暴龍', 'UTA': '爵士', 'WAS': '巫師'
}

# 深度翻譯字典
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
    'Surgery': '手術', 'Rest': '輪休', 'Health and Safety Protocols': '健康安全協議',
    'expected to': '預計將', 'out for the season': '賽季報銷', 'indefinitely': '無限期缺陣',
    'participated in': '已參加', 'practice': '訓練', 'game-time decision': '賽前決定',
    'return': '回歸', 'left': '左', 'right': '右', 'sprain': '扭傷', 'strain': '拉傷',
    'soreness': '痠痛', 'fracture': '骨折', 'recovering': '恢復中', 'torn': '撕裂'
}

def translate_text(text):
    if not text or pd.isna(text): return ""
    res = str(text)
    for eng, chi in TRANS_DICT.items():
        res = re.sub(eng, chi, res, flags=re.IGNORECASE)
    return res

st.set_page_config(page_title="NBA 數據專家 v13.2", layout="wide")

# --- 2. 傷病解析引擎 (v13.2 強化對位版) ---
@st.cache_data(ttl=600)
def get_espn_injuries_v2():
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
            
            t_name_text = title_node.get_text(strip=True)
            t_abbr = "UNKNOWN"
            for abbr, chi in TEAM_NAME_CH.items():
                if chi in t_name_text or abbr in t_name_text.upper():
                    t_abbr = abbr; break
            
            rows = table.select('tbody tr')
            for r in rows:
                cols = r.select('td')
                if len(cols) >= 3:
                    texts = [c.get_text(strip=True) for c in cols]
                    # 球員名通常在第一欄，移除位置後綴
                    p_name = re.sub(r'(PG|SG|SF|PF|C|G|F)$', '', texts[0])
                    
                    # 辨識狀態：找包含關鍵字的欄位
                    status_raw = ""
                    for txt in texts:
                        if any(k in txt for k in ['Out', 'Day-To-Day', 'GTD', 'Questionable', 'Doubtful', 'Probable']):
                            status_raw = txt; break
                    
                    # 說明通常是最後一欄
                    desc_raw = texts[-1]

                    all_inj.append({
                        '球員': p_name,
                        '狀態': translate_text(status_raw) if status_raw else "未知",
                        '說明': translate_text(desc_raw),
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
st.title("🏀 NBA 數據專家 v13.2 (錯誤修正版)")
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
                # 過濾不上的球員 (依據原始狀態判斷)
                out_names = t_inj[t_inj['RAW_STATUS'].str.contains('Out|Doubtful|Day-To-Day|GTD', case=False, na=False)]['球員'].tolist()
                all_ps = ps_db[ps_db['TEAM_ID'] == tid].sort_values('IMPACT', ascending=False)
                # 排除傷病，選取最強 8 人
                active_core = all_ps[~all_ps['PLAYER_NAME'].apply(lambda x: any(name in x for name in out_names))].head(8)
                return {'pts': active_core['PTS'].sum(), 'ts': active_core['TS_PCT'].mean(), 'pie': active_core['PIE'].mean(), 'df': active_core, 'inj_df': t_inj}

            h_res, a_res = build_team_package(h_id, h_abbr), build_team_package(a_id, a_abbr)
            final_margin = (h_res['pts']-a_res['pts'])*0.12 + (h_res['ts']-a_res['ts'])*15 + (h_res['pie']-a_res['pie'])*45 + (l10_db.get(h_id,0)-l10_db.get(a_id,0))*0.4 + 2.5
            prob_h = 1 / (1 + 10**(-final_margin/15)) * 100
            h_cn, a_cn = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
            g_key = f"v132_{dates[i].strftime('%Y%m%d')}_{a_abbr}_{h_abbr}"
            with cols[idx % 3]:
                with st.container(border=True):
                    st.markdown(f"### [客] {a_cn} vs [主] {h_cn}")
                    st.metric(f"{h_cn} 勝率", f"{prob_h:.1f}%")
                    st.metric(f"{a_cn} 勝率", f"{100-prob_h:.1f}%")
                    show_odds = st.toggle("顯示盤口分析", key=f"tog_{g_key}")
                    if show_odds:
                        c1, c2 = st.columns(2)
                        oh = c1.number_input(f"[主隊] 賠率", value=1.85, key=f"h_{g_key}", format="%.2f")
                        oa = c2.number_input(f"[客隊] 賠率", value=1.85, key=f"a_{g_key}", format="%.2f")
                        edge = (prob_h - (1/oh*100)) if prob_h > 50 else ((100-prob_h) - (1/oa*100))
                        st.info(f"💡 價值優勢: {edge:+.1f}%")
                    results.append({'label': f"[客] {a_cn} vs [主] {h_cn}", 'h_res': h_res, 'a_res': a_res, 'h_cn': h_cn, 'a_cn': a_cn})

        if results:
            st.divider()
            # 關鍵修復：增加 i 作為 key 的一部分，避免不同分頁重複 ID
            sel = st.selectbox("🔍 選擇對戰查看傷病詳情", [x['label'] for x in results], key=f"sel_detail_{i}")
            curr = next(x for x in results if x['label'] == sel)
            st.markdown("#### 🚑 ESPN 即時傷病詳情 (已翻譯說明)")
            ic1, ic2 = st.columns(2)
            with ic1:
                st.write(f"**{curr['h_cn']} 傷情**")
                if not curr['h_res']['inj_df'].empty: st.table(curr['h_res']['inj_df'][['球員', '狀態', '說明']])
                else: st.success("✅ 目前無傷病紀錄")
            with ic2:
                st.write(f"**{curr['a_cn']} 傷情**")
                if not curr['a_res']['inj_df'].empty: st.table(curr['a_res']['inj_df'][['球員', '狀態', '說明']])
                else: st.success("✅ 目前無傷病紀錄")
