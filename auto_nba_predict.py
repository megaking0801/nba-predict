import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    synergyplaytypes
)
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import pytz, warnings, re, requests, unicodedata
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from PIL import Image
import google.generativeai as genai 

# --- 1. 基本設定 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')
us_east_tz = pytz.timezone('US/Eastern')

# 完整隊名與縮寫對照 (用於比對 ESPN 資料)
TEAM_ABBR_MAP = {
    'ATL': ['Atlanta', 'Hawks'], 'BKN': ['Brooklyn', 'Nets'], 'BOS': ['Boston', 'Celtics'],
    'CHA': ['Charlotte', 'Hornets'], 'CHI': ['Chicago', 'Bulls'], 'CLE': ['Cleveland', 'Cavaliers'],
    'DAL': ['Dallas', 'Mavericks'], 'DEN': ['Denver', 'Nuggets'], 'DET': ['Detroit', 'Pistons'],
    'GSW': 'Golden State', 'HOU': 'Houston', 'IND': 'Indiana',
    'LAC': 'Clippers', 'LAL': 'Lakers', 'MEM': 'Memphis',
    'MIA': 'Miami', 'MIL': 'Milwaukee', 'MIN': 'Minnesota',
    'NOP': 'Pelicans', 'NYK': 'Knicks', 'OKC': 'Oklahoma',
    'ORL': 'Orlando', 'PHI': '76ers', 'PHX': 'Suns',
    'POR': 'Portland', 'SAC': 'Sacramento', 'SAS': 'Spurs',
    'TOR': 'Toronto', 'UTA': 'Utah', 'WAS': 'Washington'
}

TEAM_NAME_CH = {
    'ATL': '亞特蘭大老鷹', 'BKN': '布魯克林籃網', 'BOS': '波士頓塞爾提克',
    'CHA': '夏洛特黃蜂', 'CHI': '芝加哥公牛', 'CLE': '克里夫蘭騎士',
    'DAL': '達拉斯獨行俠', 'DEN': '丹佛金塊', 'DET': '底特律活塞',
    'GSW': '金州勇士', 'HOU': '休士頓火箭', 'IND': '印第安納溜馬',
    'LAC': '洛杉磯快艇', 'LAL': '洛杉磯湖人', 'MEM': '曼非斯灰熊',
    'MIA': '邁阿密熱火', 'MIL': '密爾瓦基公鹿', 'MIN': '明尼蘇達灰狼',
    'NOP': '紐奧良鵜鶘', 'NYK': '紐約尼克', 'OKC': '奧克拉荷馬雷霆',
    'ORL': '奧蘭多魔術', 'PHI': '費城 76 人', 'PHX': '鳳凰城太陽',
    'POR': '波特蘭開拓者', 'SAC': '沙加邁度國王', 'SAS': '聖安東尼奧馬刺',
    'TOR': '多倫多暴龍', 'UTA': '猶他爵士', 'WAS': '華盛頓巫師'
}

st.set_page_config(page_title="NBA 專家 v9.2 - 傷病修復版", layout="wide")

# 初始化 Session State
if 'saved_odds' not in st.session_state: st.session_state.saved_odds = {}
if 'saved_spread' not in st.session_state: st.session_state.saved_spread = {}
if 'parsed_results' not in st.session_state: st.session_state.parsed_results = {}

# --- 2. 強化版 ESPN 傷病爬蟲 ---
@st.cache_data(ttl=3600)
def get_espn_injuries_v92():
    url = "https://www.espn.com/nba/injuries"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    injury_dict = {}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # ESPN 結構：每個隊伍在一個 Table__Title 的 div 下面
        sections = soup.find_all('div', class_='Table__Title')
        
        for section in sections:
            raw_team_name = section.text.strip()
            target_abbr = None
            
            # 模糊比對隊名
            for abbr, keywords in TEAM_ABBR_MAP.items():
                if isinstance(keywords, list):
                    if any(k in raw_team_name for k in keywords):
                        target_abbr = abbr; break
                elif keywords in raw_team_name:
                    target_abbr = abbr; break
            
            if not target_abbr: continue
            
            injury_dict[target_abbr] = []
            
            # 抓取該標題後的 Table
            parent_div = section.find_parent('div', class_='ResponsiveTable')
            if not parent_div:
                parent_div = section.find_next('div', class_='ResponsiveTable')
            
            if parent_div:
                rows = parent_div.find_all('tr', class_='Table__TR')
                for row in rows:
                    cols = row.find_all('td')
                    if len(cols) >= 3:
                        p_name = cols[0].text.strip()
                        p_status = cols[2].text.strip() # ESPN 狀態通常在第三欄
                        injury_dict[target_abbr].append({'name': p_name, 'status': p_status})
    except Exception as e:
        st.error(f"⚠️ ESPN 抓取失敗: {e}")
    return injury_dict

# --- 3. 數據與模型載入 (保留 v9.1 功能) ---
def fetch_safe_df(endpoint_class, **kwargs):
    try:
        instance = endpoint_class(**kwargs)
        raw = instance.get_dict()
        res = raw['resultSets'][0] if 'resultSets' in raw else raw['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

def normalize_name(name):
    if not isinstance(name, str): return ""
    return unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode("utf-8").lower().replace('.', '').strip()

@st.cache_data(ttl=3600)
def load_all_data_v92():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S = '2025-26'
    
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    # Impact Score 公式：考慮得分與全方位數據
    ps_raw['IMPACT_SCORE'] = ps_raw['PTS'] + (ps_raw['REB'] + ps_raw['AST']) * 1.2 + ps_raw['PLUS_MINUS']
    player_impact_db = {normalize_name(row['PLAYER_NAME']): row['IMPACT_SCORE'] for _, row in ps_raw.iterrows()}
    
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST', 'IMPACT_SCORE']], ps_adv[['PLAYER_ID', 'TS_PCT']], on='PLAYER_ID')
    
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S)
    
    def to_map(df, cols): return df.set_index('TEAM_ID')[cols].to_dict('index') if not df.empty else {}
    maps = {'adv': to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE']), 'trans': to_map(df_trans, ['PPP'])}
    
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['PLUS_MINUS'] = pd.to_numeric(gf['PLUS_MINUS'], errors='coerce').fillna(0)
    gf['L10_PM'] = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(10, min_periods=3).mean()).fillna(0)
    latest_l10 = gf.groupby('TEAM_ID')['L10_PM'].last().to_dict()
    
    return ps_full, maps, player_impact_db, latest_l10, datetime.now(tw_tz).strftime("%H:%M")

ps_full, maps, player_impact_db, latest_l10, last_update = load_all_data_v92()
injuries = get_espn_injuries_v92()

# --- 4. 介面呈現 ---
st.title("🏀 NBA 數據專家 v9.2 (傷病抓取修復終極版)")

with st.sidebar:
    st.header("⚙️ 系統檢查")
    st.write(f"📊 傷病資料庫：已載入 {len(injuries)} 隊數據")
    if st.checkbox("查看傷病偵錯列表"):
        st.json(injuries)
    
    st.header("📸 截圖辨識")
    api_key = st.text_input("Gemini API Key", type="password")
    uploaded_file = st.file_uploader("上傳賠率截圖", type=['png', 'jpg', 'jpeg'])
    # (截圖辨識邏輯保持 v9.1 一致...)

nba_now = datetime.now(us_east_tz)
dates_nba = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates_nba])

for i, tab in enumerate(tabs):
    with tab:
        current_date_str = dates_nba[i].strftime('%Y-%m-%d')
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates_nba[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 無比賽資訊")
            continue

        id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        analysis_results = []

        st.subheader("💰 賠率與傷病加權 (含獨贏/讓分雙模組)")
        is_locked = st.toggle("🔒 鎖定數值", key=f"lock_{i}")
        
        with st.expander("展開輸入與即時傷情", expanded=not is_locked):
            o_cols = st.columns(3)
            idx_count = 0
            for _, row in sb.iterrows():
                h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
                h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
                if not h_abbr or not a_abbr: continue
                
                h_ch, a_ch = TEAM_NAME_CH.get(h_abbr), TEAM_NAME_CH.get(a_abbr)
                game_key = f"{current_date_str}_{a_abbr}_{h_abbr}"
                
                # --- 核心傷病權重計算 (修正) ---
                h_inj_list = injuries.get(h_abbr, [])
                a_inj_list = injuries.get(a_abbr, [])
                
                # 計算主客隊損失戰力
                def calc_loss(inj_list):
                    total_loss = 0
                    out_names = []
                    for p in inj_list:
                        status_norm = p['status'].lower()
                        # 只要狀態包含 Out, Doubtful, Injured 或是關鍵缺席字眼
                        if any(x in status_norm for x in ['out', 'doubt', 'inj', 'health']):
                            name_norm = normalize_name(p['name'])
                            impact = player_impact_db.get(name_norm, 0)
                            # 模糊匹配：若找不到全名，找姓氏
                            if impact == 0:
                                last_name = name_norm.split()[-1] if ' ' in name_norm else name_norm
                                for k, v in player_impact_db.items():
                                    if last_name in k: impact = v; break
                            total_loss += impact
                            out_names.append(p['name'])
                    return total_loss, out_names

                h_loss, h_out = calc_loss(h_inj_list)
                a_loss, a_out = calc_loss(a_inj_list)
                
                # 戰力預測
                h_l10, a_l10 = latest_l10.get(h_id, 0), latest_l10.get(a_id, 0)
                # 基礎分差 + 傷病修正 (每 10 點 Impact 約 1.5 分)
                final_m_h = (h_l10 - a_l10) * 0.7 + 2.5 + (a_loss - h_loss) * 0.15
                final_p_h = 1 / (1 + 10**(-final_m_h/15)) * 100
                
                with o_cols[idx_count % 3]:
                    st.write(f"**{a_ch} @ {h_ch}**")
                    oh = st.number_input(f"🏠 賠率", value=st.session_state.saved_odds.get(f"{game_key}_h", 1.75), key=f"ho_{game_key}", disabled=is_locked)
                    oa = st.number_input(f"✈️ 賠率", value=st.session_state.saved_odds.get(f"{game_key}_a", 1.75), key=f"ao_{game_key}", disabled=is_locked)
                    sp = st.number_input(f"🚩 讓分 (獨贏0)", value=st.session_state.saved_spread.get(f"{game_key}_sp", 0.0), key=f"sp_{game_key}", disabled=is_locked)
                    
                    if h_out: st.caption(f"🚑 {h_abbr} 缺席: {', '.join(h_out[:2])}")
                    if a_out: st.caption(f"🚑 {a_abbr} 缺席: {', '.join(a_out[:2])}")
                    
                    analysis_results.append({
                        'label': f"{a_ch} @ {h_ch}", 'h_ch': h_ch, 'a_ch': a_ch,
                        'h_id': h_id, 'a_id': a_id, 'ai_p_h': final_p_h, 'ai_m_h': final_m_h,
                        'sp': sp, 'spread_diff': final_m_h + sp, 'oh': oh, 'oa': oa,
                        'h_out': h_out, 'a_out': a_out
                    })
                idx_count += 1

        # --- v9.2 混合推薦邏輯 (確保獨贏模式下也有 Top 3) ---
        st.divider()
        st.subheader("🔥 AI 價值推薦 (Top 3)")
        
        recs = []
        for d in analysis_results:
            if d['sp'] == 0: # 獨贏模式
                implied_h = (1 / d['oh']) * 100
                implied_a = (1 / d['oa']) * 100
                edge_h = d['ai_p_h'] - implied_h
                edge_a = (100 - d['ai_p_h']) - implied_a
                if edge_h > 2: recs.append({'pick': f"{d['h_ch']} [獨贏]", 'val': edge_h, 'match': d['label'], 'desc': f"優勢: +{edge_h:.1f}%"})
                elif edge_a > 2: recs.append({'pick': f"{d['a_ch']} [獨贏]", 'val': edge_a, 'match': d['label'], 'desc': f"優勢: +{edge_a:.1f}%"})
            else: # 讓分模式
                if d['spread_diff'] > 1.0: recs.append({'pick': f"{d['h_ch']} [讓分]", 'val': d['spread_diff'], 'match': d['label'], 'desc': f"看好過盤: {d['spread_diff']:.1f}分"})
                elif d['spread_diff'] < -1.0: recs.append({'pick': f"{d['a_ch']} [讓分]", 'val': abs(d['spread_diff']), 'match': d['label'], 'desc': f"看好過盤: {abs(d['spread_diff']):.1f}分"})
        
        top_3 = sorted(recs, key=lambda x: x['val'], reverse=True)[:3]
        if top_3:
            cols = st.columns(3)
            for idx, r in enumerate(top_3):
                with cols[idx]: st.success(f"**No.{idx+1} {r['pick']}**\n\n{r['match']}\n\n{r['desc']}")
        else: st.warning("目前無高價值推薦，請手動調整賠率或等數據更新。")

        # --- 單場詳細與傷病名單 ---
        st.divider()
        if analysis_results:
            sel_label = st.selectbox("🔍 深度傷情與數據對比", [d['label'] for d in analysis_results], key=f"sel_{i}")
            curr = next(d for d in analysis_results if d['label'] == sel_label)
            
            c1, c2, c3 = st.columns(3)
            c1.metric(f"{curr['h_ch']} 勝率", f"{curr['ai_p_h']:.1f}%", f"分差: {curr['ai_m_h']:+.1f}")
            c2.metric(f"{curr['a_ch']} 勝率", f"{100-curr['ai_p_h']:.1f}%", f"分差: {-curr['ai_m_h']:+.1f}")
            
            # 完整傷病顯示
            st.write("---")
            i1, i2 = st.columns(2)
            with i1:
                st.error(f"🚑 {curr['h_ch']} 傷情")
                if curr['h_out']: st.write("、".join(curr['h_out']))
                else: st.write("✅ 無主要缺陣")
            with i2:
                st.error(f"🚑 {curr['a_ch']} 傷情")
                if curr['a_out']: st.write("、".join(curr['a_out']))
                else: st.write("✅ 無主要缺陣")

            # 數據對比
            def get_m(m, tid, k): return maps.get(m, {}).get(int(tid), {}).get(k, 0)
            st.table(pd.DataFrame({
                "指標": ["進攻效率", "防守效率", "節奏", "快攻得分(PPP)"],
                f"{curr['h_ch']}": [get_m('adv',curr['h_id'],'OFF_RATING'), get_m('adv',curr['h_id'],'DEF_RATING'), get_m('adv',curr['h_id'],'PACE'), get_m('trans',curr['h_id'],'PPP')],
                f"{curr['a_ch']}": [get_m('adv',curr['a_id'],'OFF_RATING'), get_m('adv',curr['a_id'],'DEF_RATING'), get_m('adv',curr['a_id'],'PACE'), get_m('trans',curr['a_id'],'PPP')]
            }))
