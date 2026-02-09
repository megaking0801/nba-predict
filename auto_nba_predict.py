import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, synergyplaytypes
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

# 隊名關鍵字對照 (用於 ESPN 爬蟲模糊比對)
TEAM_ABBR_MAP = {
    'ATL': 'Hawks', 'BKN': 'Nets', 'BOS': 'Celtics', 'CHA': 'Hornets',
    'CHI': 'Bulls', 'CLE': 'Cavaliers', 'DAL': 'Mavericks', 'DEN': 'Nuggets',
    'DET': 'Pistons', 'GSW': 'Warriors', 'HOU': 'Rockets', 'IND': 'Pacers',
    'LAC': 'Clippers', 'LAL': 'Lakers', 'MEM': 'Grizzlies', 'MIA': 'Heat',
    'MIL': 'Bucks', 'MIN': 'Timberwolves', 'NOP': 'Pelicans', 'NYK': 'Knicks',
    'OKC': 'Thunder', 'ORL': 'Magic', 'PHI': '76ers', 'PHX': 'Suns',
    'POR': 'Blazers', 'SAC': 'Kings', 'SAS': 'Spurs', 'TOR': 'Raptors',
    'UTA': 'Jazz', 'WAS': 'Wizards'
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

st.set_page_config(page_title="NBA 專家 v9.3", layout="wide")

if 'saved_odds' not in st.session_state: st.session_state.saved_odds = {}
if 'saved_spread' not in st.session_state: st.session_state.saved_spread = {}

# --- 2. ESPN 傷病爬蟲 (v9.2 修復版) ---
@st.cache_data(ttl=3600)
def get_espn_injuries():
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    injury_dict = {}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        sections = soup.find_all('div', class_='Table__Title')
        for section in sections:
            raw_team_name = section.text.strip()
            target_abbr = next((abbr for abbr, kw in TEAM_ABBR_MAP.items() if kw in raw_team_name), None)
            if not target_abbr: continue
            
            injury_dict[target_abbr] = []
            parent_div = section.find_parent('div', class_='ResponsiveTable') or section.find_next('div', class_='ResponsiveTable')
            if parent_div:
                for row in parent_div.find_all('tr', class_='Table__TR')[1:]:
                    cols = row.find_all('td')
                    if len(cols) >= 3:
                        injury_dict[target_abbr].append({'name': cols[0].text.strip(), 'status': cols[2].text.strip()})
    except: pass
    return injury_dict

# --- 3. 數據與核心權重 ---
def fetch_safe_df(endpoint_class, **kwargs):
    try:
        instance = endpoint_class(**kwargs); raw = instance.get_dict()
        res = raw['resultSets'][0] if 'resultSets' in raw else raw['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

def normalize_name(name):
    return unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode("utf-8").lower().replace('.', '').strip()

@st.cache_data(ttl=3600)
def load_all_data():
    S = '2025-26'
    nba_ids = [t['id'] for t in teams.get_teams()]
    
    # 球員 Impact Score (用於陣容分析)
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_raw['IMPACT_SCORE'] = ps_raw['PTS'] + (ps_raw['REB'] + ps_raw['AST']) * 1.2 + ps_raw['PLUS_MINUS']
    player_db = {normalize_name(row['PLAYER_NAME']): row['IMPACT_SCORE'] for _, row in ps_raw.iterrows()}
    
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST', 'IMPACT_SCORE']], ps_adv[['PLAYER_ID', 'TS_PCT']], on='PLAYER_ID')
    
    # 團隊數據
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S)
    maps = {'adv': df_adv.set_index('TEAM_ID').to_dict('index'), 'trans': df_trans.set_index('TEAM_ID').to_dict('index')}
    
    # L10 戰力趨勢
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['PLUS_MINUS'] = pd.to_numeric(gf['PLUS_MINUS'], errors='coerce').fillna(0)
    latest_l10 = gf.groupby('TEAM_ID')['PLUS_MINUS'].transform(lambda x: x.shift(1).rolling(10, min_periods=3).mean()).groupby(gf['TEAM_ID']).last().to_dict()
    
    return ps_full, maps, player_db, latest_l10, datetime.now(tw_tz).strftime("%H:%M")

ps_full, maps, player_db, latest_l10, last_update = load_all_data()
injuries = get_espn_injuries()

# --- 4. 主介面 ---
st.title("🏀 NBA 數據專家 v9.3 (純淨版)")

nba_now = datetime.now(us_east_tz)
dates_nba = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates_nba])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates_nba[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 無比賽"); continue

        id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        analysis_results = []

        st.subheader("💰 賠率與傷病輸入")
        is_locked = st.toggle("🔒 鎖定數值", key=f"lock_{i}")
        with st.expander("展開對戰組合", expanded=not is_locked):
            o_cols = st.columns(3)
            for idx, row in sb.iterrows():
                h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
                h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
                if not h_abbr or not a_abbr: continue
                
                h_ch, a_ch = TEAM_NAME_CH.get(h_abbr), TEAM_NAME_CH.get(a_abbr)
                g_key = f"{dates_nba[i].strftime('%Y%m%d')}_{a_abbr}_{h_abbr}"
                
                # 傷病扣分計算
                def get_loss(abbr):
                    names = []
                    loss = 0
                    for p in injuries.get(abbr, []):
                        if any(x in p['status'].lower() for x in ['out', 'doubt', 'inj']):
                            names.append(p['name'])
                            loss += player_db.get(normalize_name(p['name']), 0)
                    return loss, names

                h_loss, h_out = get_loss(h_abbr); a_loss, a_out = get_loss(a_abbr)
                
                # 預測模型：L10 戰力 + 主場優勢 + 傷病修正
                pred_margin = (latest_l10.get(h_id, 0) - latest_l10.get(a_id, 0)) * 0.7 + 2.5 + (a_loss - h_loss) * 0.15
                win_prob = 1 / (1 + 10**(-pred_margin/15)) * 100
                
                with o_cols[idx % 3]:
                    st.write(f"**{a_ch} @ {h_ch}**")
                    oh = st.number_input(f"🏠賠率", value=st.session_state.saved_odds.get(f"{g_key}_h", 1.8), key=f"ho_{g_key}", disabled=is_locked)
                    oa = st.number_input(f"✈️賠率", value=st.session_state.saved_odds.get(f"{g_key}_a", 1.8), key=f"ao_{g_key}", disabled=is_locked)
                    sp = st.number_input(f"🚩讓分", value=st.session_state.saved_spread.get(f"{g_key}_sp", 0.0), key=f"sp_{g_key}", disabled=is_locked)
                    st.session_state.saved_odds[f"{g_key}_h"], st.session_state.saved_odds[f"{g_key}_a"], st.session_state.saved_spread[f"{g_key}_sp"] = oh, oa, sp
                    
                    if h_out: st.caption(f"🚑主缺: {', '.join(h_out[:2])}")
                    if a_out: st.caption(f"🚑客缺: {', '.join(a_out[:2])}")
                    
                    analysis_results.append({
                        'label': f"{a_ch} @ {h_ch}", 'h_ch': h_ch, 'a_ch': a_ch, 'h_id': h_id, 'a_id': a_id,
                        'prob': win_prob, 'margin': pred_margin, 'sp': sp, 'oh': oh, 'oa': oa, 'h_out': h_out, 'a_out': a_out
                    })

        # --- Top 3 推薦 (獨贏/讓分混合) ---
        st.divider()
        st.subheader("🔥 AI 價值推薦")
        recs = []
        for d in analysis_results:
            if d['sp'] == 0: # 獨贏模式
                edge = (d['prob'] - (1/d['oh']*100)) if d['prob'] > 50 else ((100-d['prob']) - (1/d['oa']*100))
                pick = d['h_ch'] if d['prob'] > 50 else d['a_ch']
                if edge > 2: recs.append({'pick': f"{pick} [獨贏]", 'val': edge, 'match': d['label'], 'desc': f"價值優勢: +{edge:.1f}%"})
            else: # 讓分模式
                diff = d['margin'] + d['sp']
                if abs(diff) > 1.0:
                    pick = d['h_ch'] if diff > 0 else d['a_ch']
                    recs.append({'pick': f"{pick} [讓分]", 'val': abs(diff), 'match': d['label'], 'desc': f"過盤優勢: {abs(diff):.1f}分"})
        
        top_3 = sorted(recs, key=lambda x: x['val'], reverse=True)[:3]
        if top_3:
            cols = st.columns(3)
            for idx, r in enumerate(top_3):
                with cols[idx]: st.success(f"**No.{idx+1} {r['pick']}**\n\n{r['match']}\n\n{r['desc']}")
        else: st.warning("今日數據較平均，暫無強力推薦。")

        # --- 數據對比表 ---
        st.divider()
        if analysis_results:
            sel = st.selectbox("🔍 陣容與數據對比", [d['label'] for d in analysis_results], key=f"sel_{i}")
            curr = next(d for d in analysis_results if d['label'] == sel)
            
            # 傷病與可用核心
            c1, c2 = st.columns(2)
            for tid, name, out_list, col in [(curr['h_id'], curr['h_ch'], curr['h_out'], c1), (curr['a_id'], curr['a_ch'], curr['a_out'], c2)]:
                with col:
                    st.error(f"🚑 {name} 缺陣: {', '.join(out_list) if out_list else '無'}")
                    p_df = ps_full[ps_full['TEAM_ID'] == tid].copy()
                    p_df = p_df[~p_df['PLAYER_NAME'].isin(out_list)].sort_values('IMPACT_SCORE', ascending=False).head(5)
                    st.dataframe(p_df[['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT']].rename(columns={'PLAYER_NAME':'可用核心','TS_PCT':'命中%'}), hide_index=True)

st.sidebar.info(f"🕒 更新時間: {last_update} | 傷病庫: {len(injuries)} 隊")
