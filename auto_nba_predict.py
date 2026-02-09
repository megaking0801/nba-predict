import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    synergyplaytypes, leaguedashptdefend
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

# 擴充隊名匹配字典，防止爬蟲漏抓
TEAM_NAME_EN_MAP = {
    'Atlanta Hawks': 'ATL', 'Brooklyn Nets': 'BKN', 'Boston Celtics': 'BOS',
    'Charlotte Hornets': 'CHA', 'Chicago Bulls': 'CHI', 'Cleveland Cavaliers': 'CLE',
    'Dallas Mavericks': 'DAL', 'Denver Nuggets': 'DEN', 'Detroit Pistons': 'DET',
    'Golden State Warriors': 'GSW', 'Houston Rockets': 'HOU', 'Indiana Pacers': 'IND',
    'Los Angeles Clippers': 'LAC', 'L.A. Clippers': 'LAC', 'LA Clippers': 'LAC',
    'Los Angeles Lakers': 'LAL', 'L.A. Lakers': 'LAL', 'LA Lakers': 'LAL',
    'Memphis Grizzlies': 'MEM', 'Miami Heat': 'MIA', 'Milwaukee Bucks': 'MIL',
    'Minnesota Timberwolves': 'MIN', 'New Orleans Pelicans': 'NOP', 'New York Knicks': 'NYK',
    'Oklahoma City Thunder': 'OKC', 'Orlando Magic': 'ORL', 'Philadelphia 76ers': 'PHI',
    'Phoenix Suns': 'PHX', 'Portland Trail Blazers': 'POR', 'Sacramento Kings': 'SAC',
    'San Antonio Spurs': 'SAS', 'Toronto Raptors': 'TOR', 'Utah Jazz': 'UTA', 'Washington Wizards': 'WAS'
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

st.set_page_config(page_title="NBA 數據專家 v7.4", layout="wide")
st.title("🏀 NBA 數據專家 v7.4 (含賽季報銷名單追蹤)")

# --- 2. 爬蟲核心 (強化對報銷與隊名的抓取) ---
@st.cache_data(ttl=600)
def fetch_live_injuries_v74():
    url = "https://www.cbssports.com/nba/injuries/"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        injury_data = {}
        
        # CBS 的結構：找所有含有隊名的 div
        sections = soup.find_all('div', class_='TeamLogoNameLockup-name')
        
        for section in sections:
            raw_team_name = section.get_text().strip()
            # 模糊匹配隊名
            abbr = None
            for key, val in TEAM_NAME_EN_MAP.items():
                if key in raw_team_name or raw_team_name in key:
                    abbr = val
                    break
            
            if not abbr: continue
            
            table = section.find_next('table')
            if not table: continue
            
            rows = table.find_all('tr')[1:]
            team_injuries = []
            for row in rows:
                cols = row.find_all('td')
                if len(cols) >= 3:
                    player_name = cols[0].get_text(strip=True)
                    status_text = cols[2].get_text(strip=True)
                    # 包含報銷關鍵字判斷
                    out_keywords = ['out', 'season', 'ending', 'surgery', 'suspension', 'torn', 'fracture']
                    is_out = any(k in status_text.lower() for k in out_keywords)
                    is_dqs = any(k in status_text.lower() for k in ['day-to-day', 'questionable', 'doubtful'])
                    
                    team_injuries.append({
                        'name': player_name,
                        'status': status_text,
                        'is_out': is_out,
                        'is_dqs': is_dqs
                    })
            injury_data[abbr] = team_injuries
        return injury_data
    except Exception as e:
        st.sidebar.error(f"傷病抓取失敗: {e}")
        return {}

# --- 3. 數據與模型 ---
def normalize_name(name):
    if not isinstance(name, str): return ""
    return unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode("utf-8").lower().replace('.', '').strip()

@st.cache_data(ttl=3600)
def load_all_data_v74():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S = '2025-26'
    
    # 抓取球員數據庫 (用於比對 PPG)
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    player_stats_db = {normalize_name(row['PLAYER_NAME']): row['PTS'] for _, row in ps_raw.iterrows()} if not ps_raw.empty else {}
    
    # 基礎團隊數據 Maps (同 v7.2)
    df_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    def to_map(df, cols): return df.set_index('TEAM_ID')[cols].to_dict('index') if not df.empty else {}
    maps = {'base': to_map(df_base, ['PTS', 'REB', 'AST', 'FG_PCT']), 'adv': to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE'])}

    # 訓練預測模型
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)
    
    feats = ['REST_DAYS']
    train_df = gf.fillna(0)
    clf = xgb.XGBClassifier().fit(train_df[feats], train_df['WIN_BIN'])
    
    return clf, gf, maps, player_stats_db, datetime.now(tw_tz).strftime("%H:%M")

def fetch_safe_df(endpoint_class, **kwargs):
    try:
        instance = endpoint_class(**kwargs)
        raw = instance.get_dict()
        res = raw['resultSets'][0] if 'resultSets' in raw else raw['resultSet']
        return pd.DataFrame(res['rowSet'], columns=res['headers'])
    except: return pd.DataFrame()

clf, gf, maps, player_stats_db, last_update = load_all_data_v74()
injury_report = fetch_live_injuries_v74()

# --- 4. 介面與邏輯 ---
nba_now = datetime.now(us_east_tz)
dates_nba = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates_nba])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates_nba[i].strftime('%m/%d/%Y'))
        if sb.empty:
            st.info("📅 暫無賽程")
            continue

        id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        for _, row in sb.iterrows():
            h_abbr, a_abbr = id_to_abbr.get(row['HOME_TEAM_ID']), id_to_abbr.get(row['VISITOR_TEAM_ID'])
            if not h_abbr or not a_abbr: continue
            
            # 計算勝率
            h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
            base_prob = clf.predict_proba(h_last[feats])[0][1] * 100 if not h_last.empty else 50.0
            
            # 傷病處理
            def process_inj(abbr):
                score, details = 0, []
                injuries = injury_report.get(abbr, [])
                for inj in injuries:
                    ppg = player_stats_db.get(normalize_name(inj['name']), 0)
                    weight = 1.0 if inj['is_out'] else 0.5
                    penalty = 12 if ppg >= 25 else (7 if ppg >= 18 else (3 if ppg >= 10 else (1 if ppg >= 5 else 0)))
                    final_p = penalty * weight
                    score += final_p
                    details.append({
                        'text': f"{inj['name']} ({inj['status']}) | 場均 {ppg:.1f}分",
                        'penalty': final_p,
                        'is_out': inj['is_out']
                    })
                return score, details

            h_score, h_list = process_inj(h_abbr)
            a_score, a_list = process_inj(a_abbr)
            final_prob = max(5, min(95, base_prob - h_score + a_score))

            # 顯示
            with st.expander(f"🏀 {TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)} (預測勝率: {final_prob:.1f}%)", expanded=True):
                c1, c2 = st.columns(2)
                for col, team_ch, score, inj_list in zip([c1, c2], [TEAM_NAME_CH.get(h_abbr), TEAM_NAME_CH.get(a_abbr)], [h_score, a_score], [h_list, a_list]):
                    with col:
                        st.write(f"**{team_ch} 傷病名單** (扣分: {score:.1f}%)")
                        if not inj_list:
                            st.success("全員健康")
                        else:
                            for item in inj_list:
                                if item['penalty'] > 0:
                                    st.error(f"❌ {item['text']} [-{item['penalty']}%]")
                                else:
                                    # 報銷球員或小兵，即使不扣分也顯示
                                    st.info(f"⚪ {item['text']} [不扣分/小兵]")
                
                # 原始數據表 (精簡版)
                st.caption("團隊效率比對")
                h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
                def get_m(m, tid, k): return maps[m].get(int(tid), {}).get(k, 0)
                st.dataframe(pd.DataFrame({
                    "指標": ["進攻效率", "防守效率", "場均得分"],
                    TEAM_NAME_CH.get(h_abbr): [get_m('adv', h_id, 'OFF_RATING'), get_m('adv', h_id, 'DEF_RATING'), get_m('base', h_id, 'PTS')],
                    TEAM_NAME_CH.get(a_abbr): [get_m('adv', a_id, 'OFF_RATING'), get_m('adv', a_id, 'DEF_RATING'), get_m('base', a_id, 'PTS')]
                }), hide_index=True)

st.sidebar.write(f"📊 目前已抓取到 {sum(len(v) for v in injury_report.values())} 位傷兵數據")
