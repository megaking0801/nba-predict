import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    synergyplaytypes
)
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import pytz, warnings, requests, unicodedata, re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# --- 1. 基本設定 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')
us_east_tz = pytz.timezone('US/Eastern')

# 隊名對照表 (含 ESPN 對照)
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

# ESPN 網頁上的隊名通常是全名，需對照回縮寫
ESPN_NAME_MAP = {
    'Atlanta Hawks': 'ATL', 'Brooklyn Nets': 'BKN', 'Boston Celtics': 'BOS', 'Charlotte Hornets': 'CHA',
    'Chicago Bulls': 'CHI', 'Cleveland Cavaliers': 'CLE', 'Dallas Mavericks': 'DAL', 'Denver Nuggets': 'DEN',
    'Detroit Pistons': 'DET', 'Golden State Warriors': 'GSW', 'Houston Rockets': 'HOU', 'Indiana Pacers': 'IND',
    'LA Clippers': 'LAC', 'Los Angeles Lakers': 'LAL', 'Memphis Grizzlies': 'MEM', 'Miami Heat': 'MIA',
    'Milwaukee Bucks': 'MIL', 'Minnesota Timberwolves': 'MIN', 'New Orleans Pelicans': 'NOP', 'New York Knicks': 'NYK',
    'Oklahoma City Thunder': 'OKC', 'Orlando Magic': 'ORL', 'Philadelphia 76ers': 'PHI', 'Phoenix Suns': 'PHX',
    'Portland Trail Blazers': 'POR', 'Sacramento Kings': 'SAC', 'San Antonio Spurs': 'SAS', 'Toronto Raptors': 'TOR',
    'Utah Jazz': 'UTA', 'Washington Wizards': 'WAS'
}

st.set_page_config(page_title="NBA 專家 v8.6 - 傷病感知版", layout="wide")

# 初始化 Session State
if 'saved_odds' not in st.session_state: st.session_state.saved_odds = {}
if 'saved_spread' not in st.session_state: st.session_state.saved_spread = {}

# --- 2. ESPN 傷病爬蟲模組 (核心回歸) ---
@st.cache_data(ttl=3600)
def get_espn_injuries():
    """爬取 ESPN 傷病頁面並解析為 {Team_Abbr: [Player_Names]}"""
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0'}
    injury_dict = {}
    
    try:
        resp = requests.get(url, headers=headers)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # ESPN 結構通常是 H4 (隊名) 接 Table (球員)
        # 這裡使用更通用的查找方式
        sections = soup.find_all('div', class_='Table__Title') 
        
        current_team = None
        for section in sections:
            team_name = section.text.strip()
            # 嘗試匹配隊名
            team_abbr = ESPN_NAME_MAP.get(team_name)
            if not team_abbr:
                continue
                
            injury_dict[team_abbr] = []
            
            # 找下一個 Table
            table = section.find_next('table')
            if table:
                rows = table.find_all('tr')[1:] # 跳過表頭
                for row in rows:
                    cols = row.find_all('td')
                    if cols:
                        p_name = cols[0].text.strip()
                        status = cols[1].text.strip()
                        # 只抓確定缺席或極可能缺席的 (Out, Doubtful)
                        # Questionable (賽前決定) 暫時不列入強制扣分，但列入名單
                        injury_dict[team_abbr].append({'name': p_name, 'status': status})
                        
    except Exception as e:
        st.error(f"無法抓取傷病名單: {e}")
        return {}
        
    return injury_dict

# --- 3. 數據載入與模型 (加入球員權重計算) ---
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
def load_all_data_v86():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S = '2025-26'
    
    # 1. 球員詳細數據 (用於計算缺陣影響)
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    # 計算球員貢獻值 (這裡用 PIE 的概念：PTS + 1.5*REB + 2*AST + 2*STL + 2*BLK - 2*TOV)
    # 簡單版：PTS + (REB+AST)*1.2 + PLUS_MINUS
    ps_raw['IMPACT_SCORE'] = ps_raw['PTS'] + (ps_raw['REB'] + ps_raw['AST']) * 1.2 + ps_raw['PLUS_MINUS']
    
    # 建立球員搜尋字典
    player_impact_db = {}
    for _, row in ps_raw.iterrows():
        n_name = normalize_name(row['PLAYER_NAME'])
        player_impact_db[n_name] = row['IMPACT_SCORE']
        # 部分 ESPN 名字可能不同 (例如 Luka Doncic vs Luka Dončić)，做簡單模糊處理
        parts = n_name.split()
        if len(parts) >= 2:
            player_impact_db[f"{parts[0]} {parts[1]}"] = row['IMPACT_SCORE']

    # 完整數據表 (供顯示用)
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST', 'IMPACT_SCORE']], ps_adv[['PLAYER_ID', 'TS_PCT']], on='PLAYER_ID')
    
    # 2. 團隊數據 Maps
    df_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_hustle = fetch_safe_df(leaguehustlestatsteam.LeagueHustleStatsTeam, season=S, per_mode_time='PerGame')
    df_spd = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
    df_pass = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='Passing', per_mode_simple='PerGame')
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S)
    
    def to_map(df, cols): return df.set_index('TEAM_ID')[cols].to_dict('index') if not df.empty else {}
    maps = {'base': to_map(df_base, ['PTS', 'REB', 'AST', 'FG_PCT']), 'adv': to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE']), 'hustle': to_map(df_hustle, ['DEFLECTIONS', 'CONTESTED_SHOTS']), 'spd': to_map(df_spd, ['DIST_MILES', 'AVG_SPEED']), 'pass': to_map(df_pass, ['PASSES_MADE']), 'trans': to_map(df_trans, ['PPP'])}
    
    # 3. 基礎模型 (XGBoost) - 仍以團隊歷史數據為基底
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    
    # 計算 L10 狀態
    gf['PLUS_MINUS'] = pd.to_numeric(gf['PLUS_MINUS'], errors='coerce').fillna(0)
    gf['L10_PM'] = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(10, min_periods=3).mean()).fillna(0)
    
    clf = xgb.XGBClassifier().fit(gf[['REST_DAYS', 'L10_PM']], gf['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(gf[['REST_DAYS', 'L10_PM']], gf['PLUS_MINUS'])
    
    latest_l10 = gf.groupby('TEAM_ID')['L10_PM'].last().to_dict()
    
    return clf, reg, gf, ps_full, maps, player_impact_db, latest_l10, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, ps_full, maps, player_impact_db, latest_l10, last_update = load_all_data_v86()
injuries = get_espn_injuries() # 載入即爬取

# --- 4. 介面呈現 ---
st.title("🏀 NBA 數據專家 v8.6 (陣容感知版)")

nba_now = datetime.now(us_east_tz)
dates_nba = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates_nba])

for i, tab in enumerate(tabs):
    with tab:
        current_date_str = dates_nba[i].strftime('%Y-%m-%d')
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates_nba[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 目前無比賽資訊")
            continue

        id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        analysis_results = []

        # 輸入與計算區
        st.subheader("💰 賠率與傷病加權分析")
        is_locked = st.toggle("🔒 鎖定數值", key=f"lock_{i}")
        
        with st.expander("展開輸入 (賠率/讓分)", expanded=not is_locked):
            o_cols = st.columns(3)
            idx_count = 0
            for _, row in sb.iterrows():
                h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
                h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
                if not h_abbr or not a_abbr: continue
                
                h_ch = TEAM_NAME_CH.get(h_abbr)
                a_ch = TEAM_NAME_CH.get(a_abbr)
                game_key = f"{current_date_str}_{a_abbr}_{h_abbr}"
                
                # --- 傷病權重計算核心 ---
                # 1. 取得該隊傷兵名單
                h_inj = injuries.get(h_abbr, [])
                a_inj = injuries.get(a_abbr, [])
                
                # 2. 計算「缺陣扣分」 (Missing Impact)
                # 若球員狀態包含 'Out' 或 'Doubtful'，則扣除其 Impact Score
                h_missing_score = 0
                h_out_names = []
                for p in h_inj:
                    if any(x in p['status'] for x in ['Out', 'Doubtful', 'Injured']):
                        p_n = normalize_name(p['name'])
                        score = player_impact_db.get(p_n, 0)
                        # 如果找不到完全匹配，嘗試用姓氏匹配
                        if score == 0:
                            for db_name, db_score in player_impact_db.items():
                                if p_n in db_name: score = db_score; break
                        h_missing_score += score
                        h_out_names.append(p['name'])

                a_missing_score = 0
                a_out_names = []
                for p in a_inj:
                    if any(x in p['status'] for x in ['Out', 'Doubtful', 'Injured']):
                        p_n = normalize_name(p['name'])
                        score = player_impact_db.get(p_n, 0)
                        if score == 0:
                            for db_name, db_score in player_impact_db.items():
                                if p_n in db_name: score = db_score; break
                        a_missing_score += score
                        a_out_names.append(p['name'])

                # 3. 調整預測邏輯
                # 原始預測 (基於 L10 和 Rest)
                h_l10 = latest_l10.get(h_id, 0.0)
                a_l10 = latest_l10.get(a_id, 0.0)
                
                # 基礎分差
                base_diff = (h_l10 - a_l10) * 0.7 + 2.5 # 主場優勢 2.5
                
                # 傷病修正 (Impact Adjustment)
                # 假設 Impact Score 每 10 點 約等於 1 分的分差影響 (經驗係數)
                impact_adj = (a_missing_score - h_missing_score) * 0.15 
                
                final_m_h = base_diff + impact_adj
                
                # 轉換成勝率 (Sigmoid 近似)
                final_p_h = 1 / (1 + 10**(-final_m_h/15)) * 100 
                
                # --- UI 輸入 ---
                with o_cols[idx_count % 3]:
                    st.write(f"**{a_ch} [客] @ {h_ch} [主]**")
                    oh = st.number_input(f"🏠 {h_abbr} 賠率", value=st.session_state.saved_odds.get(f"{game_key}_h", 1.75), key=f"ho_{game_key}", disabled=is_locked)
                    oa = st.number_input(f"✈️ {a_abbr} 賠率", value=st.session_state.saved_odds.get(f"{game_key}_a", 1.75), key=f"ao_{game_key}", disabled=is_locked)
                    sp = st.number_input(f"🚩 主讓分", value=st.session_state.saved_spread.get(f"{game_key}_sp", -1.5), key=f"sp_{game_key}", disabled=is_locked)
                    
                    st.session_state.saved_odds[f"{game_key}_h"] = oh
                    st.session_state.saved_odds[f"{game_key}_a"] = oa
                    st.session_state.saved_spread[f"{game_key}_sp"] = sp
                    
                    # 顯示傷病摘要
                    if h_out_names: st.caption(f"🚑 主缺: {', '.join(h_out_names[:2])} 等")
                    if a_out_names: st.caption(f"🚑 客缺: {', '.join(a_out_names[:2])} 等")

                analysis_results.append({
                    'label': f"{a_ch} [客] @ {h_ch} [主]",
                    'h_ch': h_ch, 'a_ch': a_ch,
                    'h_id': h_id, 'a_id': a_id,
                    'ai_p_h': final_p_h, 'ai_m_h': final_m_h,
                    'sp': sp, 'spread_diff': final_m_h + sp,
                    'h_inj': h_out_names, 'a_inj': a_out_names
                })
                idx_count += 1

        # --- 推薦與表格 ---
        st.divider()
        st.subheader("🔥 陣容加權後推薦")
        recs = []
        for d in analysis_results:
            pick_type = "讓分" if d['sp'] != 0 else "獨贏"
            adv_score = d['spread_diff'] if d['sp'] != 0 else (d['ai_p_h'] - 50)/5 # 獨贏轉換分數
            
            if d['spread_diff'] > 1.5: recs.append({'pick': d['h_ch'], 'val': abs(d['spread_diff']), 'match': d['label'], 'note': '主隊優勢'})
            elif d['spread_diff'] < -1.5: recs.append({'pick': d['a_ch'], 'val': abs(d['spread_diff']), 'match': d['label'], 'note': '客隊優勢'})
            
        top_3 = sorted(recs, key=lambda x: x['val'], reverse=True)[:3]
        rc1, rc2, rc3 = st.columns(3)
        for idx, r in enumerate(top_3):
            with [rc1, rc2, rc3][idx]:
                st.success(f"**No.{idx+1} {r['pick']}**\n\n{r['match']}\n\n預測優勢: {r['val']:.1f}")

        # --- 單場深度表格 (包含傷病名單) ---
        st.divider()
        sel_label = st.selectbox("🔍 陣容與數據對比", [d['label'] for d in analysis_results], key=f"sel_{i}")
        curr = next(d for d in analysis_results if d['label'] == sel_label)
        
        c1, c2, c3 = st.columns(3)
        c1.metric(f"{curr['h_ch']} 勝率", f"{curr['ai_p_h']:.1f}%", f"分差: {curr['ai_m_h']:+.1f}")
        c2.metric("莊家盤口", f"{curr['sp']:+.1f}")
        c3.warning(f"🚑 傷病修正影響: {(curr['ai_m_h'] - ((latest_l10.get(curr['h_id'],0)-latest_l10.get(curr['a_id'],0))*0.7 + 2.5)):+.1f} 分")

        # 傷病名單顯示
        col_inj1, col_inj2 = st.columns(2)
        with col_inj1:
            st.error(f"**{curr['h_ch']} 傷病/缺席名單**")
            if curr['h_inj']: st.write(", ".join(curr['h_inj']))
            else: st.write("無主要傷兵 (或未更新)")
            
        with col_inj2:
            st.error(f"**{curr['a_ch']} 傷病/缺席名單**")
            if curr['a_inj']: st.write(", ".join(curr['a_inj']))
            else: st.write("無主要傷兵 (或未更新)")

        # 核心數據表 (保留 User 喜歡的表格)
        def get_m(m, tid, k): return maps.get(m, {}).get(int(tid), {}).get(k, 0)
        st.table(pd.DataFrame({
            "指標": ["進攻效率", "防守效率", "近況 L10", "節奏", "失誤得分"],
            f"{curr['h_ch']}": [get_m('adv',curr['h_id'],'OFF_RATING'), get_m('adv',curr['h_id'],'DEF_RATING'), f"{latest_l10.get(curr['h_id'],0):.1f}", get_m('adv',curr['h_id'],'PACE'), get_m('trans',curr['h_id'],'PPP')],
            f"{curr['a_ch']}": [get_m('adv',curr['a_id'],'OFF_RATING'), get_m('adv',curr['a_id'],'DEF_RATING'), f"{latest_l10.get(curr['a_id'],0):.1f}", get_m('adv',curr['a_id'],'PACE'), get_m('trans',curr['a_id'],'PPP')]
        }))

        # 可用核心球員 (自動過濾傷兵)
        p1, p2 = st.columns(2)
        for tid, name, inj_list, col in [(curr['h_id'], curr['h_ch'], curr['h_inj'], p1), (curr['a_id'], curr['a_ch'], curr['a_inj'], p2)]:
            with col:
                st.write(f"**{name} 可用核心 (已濾除傷兵)**")
                # 篩選掉在傷病名單中的球員
                p_df = ps_full[ps_full['TEAM_ID'] == tid].copy()
                # 簡單過濾：名字若出現在 inj_list 則排除
                # 注意：這裡用模糊比對，因為 full name 可能有差異
                p_df['IS_OUT'] = p_df['PLAYER_NAME'].apply(lambda x: any(inj_name in x or x in inj_name for inj_name in inj_list))
                p_df = p_df[~p_df['IS_OUT']].sort_values('PTS', ascending=False).head(6)
                
                st.dataframe(p_df[['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分','TS_PCT':'真實命中%'}), hide_index=True)

st.sidebar.info(f"🕒 更新時間：{last_update}")
