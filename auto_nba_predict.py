import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    synergyplaytypes
)
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import pytz, warnings, requests, unicodedata
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# --- 1. 基本設定 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')
us_east_tz = pytz.timezone('US/Eastern')

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

TEAM_NAME_EN_MAP = {
    'Atlanta': 'ATL', 'Brooklyn': 'BKN', 'Boston': 'BOS', 'Charlotte': 'CHA',
    'Chicago': 'CHI', 'Cleveland': 'CLE', 'Dallas': 'DAL', 'Denver': 'DEN',
    'Detroit': 'DET', 'Golden State': 'GSW', 'Houston': 'HOU', 'Indiana': 'IND',
    'LA Clippers': 'LAC', 'LA Lakers': 'LAL', 'Memphis': 'MEM', 'Miami': 'MIA',
    'Milwaukee': 'MIL', 'Minnesota': 'MIN', 'New Orleans': 'NOP', 'New York': 'NYK',
    'Oklahoma City': 'OKC', 'Orlando': 'ORL', 'Philadelphia': 'PHI', 'Phoenix': 'PHX',
    'Portland': 'POR', 'Sacramento': 'SAC', 'San Antonio': 'SAS', 'Toronto': 'TOR',
    'Utah': 'UTA', 'Washington': 'WAS'
}

st.set_page_config(page_title="NBA 專家 v8.3 - 讓分模式與主客標註", layout="wide")

# 初始化 Session State
if 'saved_odds' not in st.session_state: st.session_state.saved_odds = {}
if 'saved_spread' not in st.session_state: st.session_state.saved_spread = {}

# --- 2. 數據載入 (保持完整) ---
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
def load_all_data_v83():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S = '2025-26'
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST']], ps_adv[['PLAYER_ID', 'TS_PCT']], on='PLAYER_ID')
    player_db = {normalize_name(row['PLAYER_NAME']): row['PTS'] for _, row in ps_full.iterrows()}
    
    # Maps
    df_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_hustle = fetch_safe_df(leaguehustlestatsteam.LeagueHustleStatsTeam, season=S, per_mode_time='PerGame')
    df_spd = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
    df_pass = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='Passing', per_mode_simple='PerGame')
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S)
    
    def to_map(df, cols): return df.set_index('TEAM_ID')[cols].to_dict('index') if not df.empty else {}
    maps = {'base': to_map(df_base, ['PTS', 'REB', 'AST', 'FG_PCT']), 'adv': to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE']), 'hustle': to_map(df_hustle, ['DEFLECTIONS', 'CONTESTED_SHOTS']), 'spd': to_map(df_spd, ['DIST_MILES', 'AVG_SPEED']), 'pass': to_map(df_pass, ['PASSES_MADE']), 'trans': to_map(df_trans, ['PPP'])}
    
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    
    clf = xgb.XGBClassifier().fit(gf[['REST_DAYS']].fillna(0), gf['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(gf[['REST_DAYS']].fillna(0), gf['PLUS_MINUS'].fillna(0))
    return clf, reg, gf, ps_full, maps, player_db, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, ps_full, maps, player_db, last_update = load_all_data_v83()

# --- 3. 介面 ---
st.title("🏀 NBA 數據專家 v8.3 (主客場全標註版)")

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

        # --- 賠率與讓分鎖定區 (加入主客提示) ---
        st.subheader("💰 賠率與讓分輸入")
        is_locked = st.toggle("🔒 鎖定數值", key=f"lock_{i}")
        
        with st.expander("展開輸入當前數值", expanded=not is_locked):
            o_cols = st.columns(3)
            idx_count = 0
            for _, row in sb.iterrows():
                h_abbr, a_abbr = id_to_abbr.get(row['HOME_TEAM_ID']), id_to_abbr.get(row['VISITOR_TEAM_ID'])
                if not h_abbr or not a_abbr: continue
                game_key = f"{current_date_str}_{a_abbr}_{h_abbr}"
                
                with o_cols[idx_count % 3]:
                    st.write(f"**{TEAM_NAME_CH.get(a_abbr)} [客] @ {TEAM_NAME_CH.get(h_abbr)} [主]**")
                    oh = st.number_input(f"🏠 {h_abbr} [主] 賠率", value=st.session_state.saved_odds.get(f"{game_key}_h", 1.75), step=0.01, key=f"ho_{game_key}", disabled=is_locked)
                    oa = st.number_input(f"✈️ {a_abbr} [客] 賠率", value=st.session_state.saved_odds.get(f"{game_key}_a", 1.75), step=0.01, key=f"ao_{game_key}", disabled=is_locked)
                    sp = st.number_input(f"🚩 主隊讓分 (獨贏填0)", value=st.session_state.saved_spread.get(f"{game_key}_sp", -1.5), step=0.5, key=f"sp_{game_key}", disabled=is_locked)
                    
                    if sp == 0: st.caption("ℹ️ 目前模式：不讓分 (獨贏)")
                    else: st.caption(f"ℹ️ 目前模式：讓分盤 (主隊{'讓' if sp<0 else '受讓'} {abs(sp)})")
                    
                    st.session_state.saved_odds[f"{game_key}_h"], st.session_state.saved_odds[f"{game_key}_a"], st.session_state.saved_spread[f"{game_key}_sp"] = oh, oa, sp
                
                # AI 計算
                h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
                ai_p_h = clf.predict_proba(h_last[['REST_DAYS']])[0][1]*100 if not h_last.empty else 50.0
                ai_m_h = reg.predict(h_last[['REST_DAYS']])[0] if not h_last.empty else 0.0
                
                analysis_results.append({
                    'label': f"{TEAM_NAME_CH.get(a_abbr)} [客] @ {TEAM_NAME_CH.get(h_abbr)} [主]",
                    'h_ch': TEAM_NAME_CH.get(h_abbr), 'a_ch': TEAM_NAME_CH.get(a_abbr),
                    'h_id': row['HOME_TEAM_ID'], 'a_id': row['VISITOR_TEAM_ID'], 'ai_p_h': ai_p_h, 'ai_m_h': ai_m_h,
                    'sp': sp, 'spread_diff': ai_m_h + sp, 'oh': oh, 'oa': oa
                })
                idx_count += 1

        # --- Top 3 推薦 (加入主客提示) ---
        st.divider()
        st.subheader("🔥 AI 推薦：最佳過盤組合")
        recs = []
        for d in analysis_results:
            if d['spread_diff'] > 1.0: recs.append({'pick': f"{d['h_ch']} [主]", 'val': d['spread_diff'], 'match': d['label']})
            elif d['spread_diff'] < -1.0: recs.append({'pick': f"{d['a_ch']} [客]", 'val': abs(d['spread_diff']), 'match': d['label']})
        
        top_3 = sorted(recs, key=lambda x: x['val'], reverse=True)[:3]
        rc1, rc2, rc3 = st.columns(3)
        for idx, r in enumerate(top_3):
            with [rc1, rc2, rc3][idx]:
                st.warning(f"**No.{idx+1} {r['pick']}**\n\n{r['match']}\n\n預測優勢: {r['val']:.1f} 分")

        # --- 單場深度分析 (主客對照) ---
        st.divider()
        sel_label = st.selectbox("🔍 詳細數據對比", [d['label'] for d in analysis_results], key=f"sel_{i}")
        curr = next(d for d in analysis_results if d['label'] == sel_label)
        
        c1, c2, c3 = st.columns(3)
        c1.metric(f"{curr['h_ch']} [主]", f"{curr['ai_p_h']:.1f}%", f"預測分差: {curr['ai_m_h']:+.1f}")
        c2.metric(f"{curr['a_ch']} [客]", f"{100-curr['ai_p_h']:.1f}%", f"預測分差: {-curr['ai_m_h']:+.1f}")
        
        diff = curr['spread_diff']
        adv_text = f"🔥 {curr['h_ch']} 過盤" if diff > 2 else (f"✅ {curr['h_ch']} 過盤" if diff > 0.5 else (f"🔥 {curr['a_ch']} 過盤" if diff < -2 else (f"✅ {curr['a_ch']} 過盤" if diff < -0.5 else "⚠️ 建議避開")))
        c3.subheader(adv_text)

        # 進階數據表格
        def get_m(m, tid, k): return maps.get(m, {}).get(int(tid), {}).get(k, 0)
        st.table(pd.DataFrame({
            "指標項目": ["進攻效率", "防守效率", "節奏", "轉換進攻(PPP)", "場均傳球", "干擾投籃", "撥球"],
            f"{curr['h_ch']} [主]": [get_m('adv',curr['h_id'],'OFF_RATING'), get_m('adv',curr['h_id'],'DEF_RATING'), get_m('adv',curr['h_id'],'PACE'), get_m('trans',curr['h_id'],'PPP'), get_m('pass',curr['h_id'],'PASSES_MADE'), get_m('hustle',curr['h_id'],'CONTESTED_SHOTS'), get_m('hustle',curr['h_id'],'DEFLECTIONS')],
            f"{curr['a_ch']} [客]": [get_m('adv',curr['a_id'],'OFF_RATING'), get_m('adv',curr['a_id'],'DEF_RATING'), get_m('adv',curr['a_id'],'PACE'), get_m('trans',curr['a_id'],'PPP'), get_m('pass',curr['a_id'],'PASSES_MADE'), get_m('hustle',curr['a_id'],'CONTESTED_SHOTS'), get_m('hustle',curr['a_id'],'DEFLECTIONS')]
        }))
        
        # 核心球員
        p1, p2 = st.columns(2)
        for tid, name, tag, col in [(curr['h_id'], curr['h_ch'], "[主]", p1), (curr['a_id'], curr['a_ch'], "[客]", p2)]:
            with col:
                st.write(f"**{name} {tag} 核心球員**")
                p_df = ps_full[ps_full['TEAM_ID'] == tid].sort_values('PTS', ascending=False).head(6)
                st.dataframe(p_df[['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分','TS_PCT':'真實命中%'}), hide_index=True)

st.sidebar.info(f"🕒 數據更新：{last_update}")
