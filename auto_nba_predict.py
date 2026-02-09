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
import time, random

# =========================
# 1) 基本設定
# =========================
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

st.set_page_config(page_title="NBA 數據專家 v8.1", layout="wide")
st.title("🏀 NBA 數據專家 v8.1（ESPN + NBA官方傷病交叉比對）")

# =========================
# 2) 工具函數
# =========================
def normalize_name(name):
    if not isinstance(name, str):
        return ""
    # 去重音/統一大小寫/去點
    return unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode("utf-8").lower().replace('.', '').strip()

NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "Connection": "keep-alive",
}

def fetch_safe_df(endpoint_class, result_set_name=None, result_set_index=0,
                  max_retries=5, base_sleep=1.1, timeout=25, **kwargs):
    """
    - 支援 ScoreboardV2 resultSets 用 result_set_name 取特定表（如 LineScore / InactivePlayers）
    - 加 headers + retry/backoff，提高雲端成功率
    回傳 DataFrame；失敗回空 df
    """
    last_err = None
    for attempt in range(max_retries):
        try:
            instance = endpoint_class(headers=NBA_HEADERS, timeout=timeout, **kwargs)
            raw = instance.get_dict()

            if 'resultSets' in raw:
                rs_list = raw['resultSets']
                if result_set_name is not None:
                    rs = next((x for x in rs_list if x.get('name') == result_set_name), None)
                    if rs is None:
                        return pd.DataFrame()
                else:
                    rs = rs_list[result_set_index]
            else:
                rs = raw.get('resultSet', None)
                if rs is None:
                    return pd.DataFrame()

            df = pd.DataFrame(rs['rowSet'], columns=rs['headers'])
            return df

        except Exception as e:
            last_err = str(e)
            sleep_s = base_sleep * (2 ** attempt) + random.uniform(0, 0.6)
            time.sleep(sleep_s)

    return pd.DataFrame()

# =========================
# 2.1) ESPN 傷病（原本）
# =========================
@st.cache_data(ttl=600)
def fetch_live_injuries_espn():
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')

        injury_data = {}
        sections = soup.find_all(class_='Table__Title')
        for section in sections:
            team_raw = section.get_text().strip()
            abbr = next((a for n, a in TEAM_NAME_EN_MAP.items() if n in team_raw), None)
            if not abbr:
                continue

            table = section.find_next('table')
            rows = table.find_all('tr')[1:]
            team_inj = []
            for row in rows:
                cols = row.find_all('td')
                if len(cols) >= 3:
                    name = cols[0].get_text(strip=True)
                    status = cols[2].get_text(strip=True)

                    status_l = status.lower()
                    is_out = any(k in status_l for k in ['out', 'season', 'surgery', 'indefinitely', 'broken', 'torn'])
                    is_doubtful = any(k in status_l for k in ['doubtful'])
                    is_questionable = any(k in status_l for k in ['questionable'])
                    is_day_to_day = any(k in status_l for k in ['day-to-day', 'day to day', 'dtd'])

                    team_inj.append({
                        'name': name,
                        'status': status,
                        'espn_out': bool(is_out),
                        'espn_doubtful': bool(is_doubtful),
                        'espn_questionable': bool(is_questionable),
                        'espn_dtd': bool(is_day_to_day),
                    })

            injury_data[abbr] = team_inj

        return injury_data
    except:
        return {}

# =========================
# 2.2) NBA 官方傷病（ScoreboardV2 InactivePlayers）
# =========================
@st.cache_data(ttl=120)
def fetch_nba_official_inactives(game_date_mmddyyyy: str):
    """
    NBA 官方：以當日賽程的 InactivePlayers 為準（這場確定不打/不啟用）
    回傳 dict: team_abbr -> list[{name, nba_inactive=True}]
    """
    try:
        inactive = fetch_safe_df(
            scoreboardv2.ScoreboardV2,
            game_date=game_date_mmddyyyy,
            result_set_name="InactivePlayers",
            max_retries=5,
            timeout=25
        )
        if inactive.empty or "TEAM_ID" not in inactive.columns:
            return {}

        id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}

        # 常見欄位：PLAYER_NAME / TEAM_ID / PLAYER_ID
        name_col = "PLAYER_NAME" if "PLAYER_NAME" in inactive.columns else None
        if name_col is None:
            return {}

        out = {}
        for tid, g in inactive.groupby("TEAM_ID"):
            abbr = id_to_abbr.get(int(tid))
            if not abbr:
                continue
            out[abbr] = [{'name': n, 'nba_inactive': True} for n in g[name_col].dropna().astype(str).tolist()]

        return out
    except:
        return {}

# =========================
# 2.3) 傷病交叉比對 merge
# =========================
def merge_injury_sources(espn_report: dict, nba_inactive_report: dict):
    """
    合併來源到同一份 team->players dict
    每位球員結構：
      {
        'name': str,
        'nba_inactive': bool,
        'espn_status': str|None,
        'espn_out': bool,
        'espn_doubtful': bool,
        'espn_questionable': bool,
        'espn_dtd': bool
      }
    key 用 normalize_name 做對齊
    """
    merged = {}

    # 先把 ESPN 放入
    for abbr, plist in (espn_report or {}).items():
        merged.setdefault(abbr, {})
        for p in plist:
            key = normalize_name(p.get('name', ''))
            if not key:
                continue
            merged[abbr].setdefault(key, {
                'name': p.get('name', ''),
                'nba_inactive': False,
                'espn_status': p.get('status', None),
                'espn_out': bool(p.get('espn_out', False)),
                'espn_doubtful': bool(p.get('espn_doubtful', False)),
                'espn_questionable': bool(p.get('espn_questionable', False)),
                'espn_dtd': bool(p.get('espn_dtd', False)),
            })

    # 再把 NBA Inactive 疊上去（同名就標記 nba_inactive=True；不同名就新增）
    for abbr, plist in (nba_inactive_report or {}).items():
        merged.setdefault(abbr, {})
        for p in plist:
            key = normalize_name(p.get('name', ''))
            if not key:
                continue
            if key in merged[abbr]:
                merged[abbr][key]['nba_inactive'] = True
            else:
                merged[abbr][key] = {
                    'name': p.get('name', ''),
                    'nba_inactive': True,
                    'espn_status': None,
                    'espn_out': False,
                    'espn_doubtful': False,
                    'espn_questionable': False,
                    'espn_dtd': False,
                }

    return merged

# =========================
# 3) 數據核心：模型與資料（保留你 v8.0 結構）
# =========================
@st.cache_data(ttl=3600)
def load_all_data_v81():
    nba_ids = [t['id'] for t in teams.get_teams()]
    S, ST = '2025-26', 'Regular Season'

    # 球員數據
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')

    if ps_raw.empty or ps_adv.empty:
        ps_full = pd.DataFrame()
        player_stats_db = {}
    else:
        ps_full = pd.merge(
            ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST']],
            ps_adv[['PLAYER_ID', 'TS_PCT']],
            on='PLAYER_ID'
        )
        player_stats_db = {normalize_name(row['PLAYER_NAME']): float(row['PTS']) for _, row in ps_full.iterrows()}

    # 團隊 Maps（抓不到就空 map，不讓 app 死）
    df_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_hustle = fetch_safe_df(leaguehustlestatsteam.LeagueHustleStatsTeam, season=S, per_mode_time='PerGame')
    df_spd = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
    df_pass = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='Passing', per_mode_simple='PerGame')
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)

    def to_map(df, cols):
        if df.empty or 'TEAM_ID' not in df.columns:
            return {}
        keep = [c for c in cols if c in df.columns]
        if not keep:
            return {}
        return df.set_index('TEAM_ID')[keep].to_dict('index')

    maps = {
        'base': to_map(df_base, ['PTS', 'REB', 'AST', 'FG_PCT']),
        'adv': to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE']),
        'hustle': to_map(df_hustle, ['DEFLECTIONS', 'CONTESTED_SHOTS']),
        'spd': to_map(df_spd, ['DIST_MILES', 'AVG_SPEED']),
        'pass': to_map(df_pass, ['PASSES_MADE']),
        'trans': to_map(df_trans, ['PPP'])
    }

    # GameFinder：訓練勝率/勝分差
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    if gf_raw.empty:
        # 讓 UI 可以顯示錯誤而不是直接 crash
        return None, None, pd.DataFrame(), ps_full, [], maps, player_stats_db, datetime.now(tw_tz).strftime("%H:%M")

    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'])
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)

    feats = ['REST_DAYS']
    clf = xgb.XGBClassifier().fit(gf[feats].fillna(0), gf['WIN_BIN'])
    reg = xgb.XGBRegressor().fit(gf[feats].fillna(0), gf['PLUS_MINUS'].fillna(0))

    return clf, reg, gf, ps_full, feats, maps, player_stats_db, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, ps_full, feats, maps, player_stats_db, last_update = load_all_data_v81()

if gf is None or gf.empty or clf is None or reg is None:
    st.error("目前 NBA API 資料抓取失敗（GameFinder 取不到）。")
    st.stop()

# =========================
# 4) 傷病影響計分（交叉比對版）
# =========================
def base_penalty_from_ppg(ppg: float) -> float:
    # 你原本的分段邏輯：這裡保留
    if ppg >= 25: return 12
    if ppg >= 18: return 7
    if ppg >= 10: return 3
    if ppg >= 5:  return 1
    return 0

def injury_weight(p):
    """
    交叉比對權重：
    - NBA 官方 inactive：1.0（這場確定不打）
    - ESPN out/season/surgery：0.85
    - ESPN doubtful：0.60
    - ESPN questionable：0.40
    - ESPN dtd：0.25
    - 都沒有：0
    """
    if p.get('nba_inactive', False):
        return 1.0
    if p.get('espn_out', False):
        return 0.85
    if p.get('espn_doubtful', False):
        return 0.60
    if p.get('espn_questionable', False):
        return 0.40
    if p.get('espn_dtd', False):
        return 0.25
    return 0.0

def get_injury_impact_from_merged(team_abbr: str, merged_report: dict, db: dict):
    """
    回傳 (impact_score_percent, details_list, nba_inactive_ids_set_for_team)
    impact_score_percent 用於你後面的 final_p 修正（以 % 表示）
    """
    team_players = merged_report.get(team_abbr, {})
    score = 0.0
    details = []

    for _, p in team_players.items():
        nm = p.get('name', '')
        ppg = float(db.get(normalize_name(nm), 0.0))
        base = base_penalty_from_ppg(ppg)
        w = injury_weight(p)
        penalty = base * w

        if penalty <= 0:
            continue

        # 顯示來源
        src = []
        if p.get('nba_inactive', False): src.append("NBA官方(Inactive)")
        if p.get('espn_status', None): src.append(f"ESPN:{p['espn_status']}")
        src_txt = " | ".join(src) if src else "ESPN"

        icon = "❌" if p.get('nba_inactive', False) or p.get('espn_out', False) else "⚠️"
        details.append(f"{icon} {nm}（{ppg:.1f} PPG）影響 -{penalty:.1f}% 〔{src_txt}〕")
        score += penalty

    # 不讓傷病修正太誇張：上限 35%
    score = min(35.0, score)
    return score, details

# =========================
# 5) 介面主體
# =========================
st.title("🏀 NBA 數據專家 v8.1 (ESPN + NBA官方傷病交叉比對)")

nba_now = datetime.now(us_east_tz)
dates_nba = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime('%m/%d') for d in dates_nba])

# 預先抓 ESPN（整站）
espn_report = fetch_live_injuries_espn()

for i, tab in enumerate(tabs):
    with tab:
        game_date_str = dates_nba[i].strftime('%m/%d/%Y')

        # NBA 官方：當日 InactivePlayers
        nba_inactive_report = fetch_nba_official_inactives(game_date_str)

        # 合併交叉比對
        merged_report = merge_injury_sources(espn_report, nba_inactive_report)

        # ScoreboardV2：用 LineScore 取對戰（你原本直接抓第一張表，有時不是賽程表）
        line_score = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=game_date_str, result_set_name="LineScore")
        if line_score.empty:
            st.info("📅 目前無比賽資訊")
            continue

        id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}

        game_list = []
        for _, row in line_score.iterrows():
            h_id, a_id = int(row['HOME_TEAM_ID']), int(row['VISITOR_TEAM_ID'])
            h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
            if h_abbr and a_abbr:
                game_list.append({
                    'label': f"{TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}",
                    'h_id': h_id, 'a_id': a_id, 'h_abbr': h_abbr, 'a_abbr': a_abbr
                })

        # A. 串關賠率批次輸入
        st.subheader("💰 當日賠率批次輸入 (用於計算 Edge)")
        with st.expander("展開輸入當前運彩賠率", expanded=True):
            input_odds = {}
            o_cols = st.columns(3)
            for idx, g in enumerate(game_list):
                with o_cols[idx % 3]:
                    st.write(f"**{g['label']}**")
                    oh = st.number_input(f"🏠 {TEAM_NAME_CH.get(g['h_abbr'])}", value=1.75, step=0.01, key=f"oh_{idx}_{i}")
                    oa = st.number_input(f"✈️ {TEAM_NAME_CH.get(g['a_abbr'])}", value=1.75, step=0.01, key=f"oa_{idx}_{i}")
                    input_odds[idx] = (oh, oa)

        # B. 預測 + 傷病交叉修正 + Edge
        analysis_data = []
        for idx, g in enumerate(game_list):
            h_last = gf[gf['TEAM_ABBREVIATION'] == g['h_abbr']].tail(1)

            # 基礎勝率與分差（你現有模型只用 REST_DAYS）
            base_p = float(clf.predict_proba(h_last[feats])[0][1] * 100) if not h_last.empty else 50.0
            base_m = float(reg.predict(h_last[feats])[0]) if not h_last.empty else 0.0

            # 傷病：交叉比對後的 impact
            h_imp, h_det = get_injury_impact_from_merged(g['h_abbr'], merged_report, player_stats_db)
            a_imp, a_det = get_injury_impact_from_merged(g['a_abbr'], merged_report, player_stats_db)

            # 勝率修正（你的原式：base_p - h_imp + a_imp）
            final_p_h = max(5, min(95, base_p - h_imp + a_imp))

            # 勝分差修正：讓缺陣影響更線性（你原本 /3 我保留，但用交叉 impact）
            final_m_h = base_m - (h_imp / 3.0) + (a_imp / 3.0)

            oh, oa = input_odds[idx]

            # bookmaker implied prob（去水）
            imp_h = (1/oh) / ((1/oh) + (1/oa)) * 100
            imp_a = (1/oa) / ((1/oh) + (1/oa)) * 100

            edge_h = final_p_h - imp_h
            edge_a = (100 - final_p_h) - imp_a

            analysis_data.append({
                'label': g['label'],
                'h_ch': TEAM_NAME_CH.get(g['h_abbr']),
                'a_ch': TEAM_NAME_CH.get(g['a_abbr']),
                'final_p_h': final_p_h,
                'final_m_h': final_m_h,
                'edge_h': edge_h,
                'edge_a': edge_a,
                'odds_h': oh,
                'odds_a': oa,
                'h_id': g['h_id'],
                'a_id': g['a_id'],
                'h_abbr': g['h_abbr'],
                'a_abbr': g['a_abbr'],
                'h_det': h_det,
                'a_det': a_det
            })

        # C. Top 3 推薦
        st.divider()
        st.subheader("🔥 AI 推薦串關最優三場")
        recs = []
        for d in analysis_data:
            if d['edge_h'] > d['edge_a']:
                recs.append({'pick': d['h_ch'], 'edge': d['edge_h'], 'match': d['label'], 'odds': d['odds_h']})
            else:
                recs.append({'pick': d['a_ch'], 'edge': d['edge_a'], 'match': d['label'], 'odds': d['odds_a']})

        top_3 = sorted(recs, key=lambda x: x['edge'], reverse=True)[:3]
        rc1, rc2, rc3 = st.columns(3)
        for idx, r in enumerate(top_3):
            with [rc1, rc2, rc3][idx]:
                st.success(f"**No.{idx+1} {r['pick']}**\n\n{r['match']}\n\n價值: +{r['edge']:.1f}% | 賠率: {r['odds']}")

        # D. 單場詳細
        st.divider()
        sel_label = st.selectbox("🔍 選擇場次查看「AI 勝率」與「勝分差預測」", [d['label'] for d in analysis_data], key=f"sel_final_{i}")
        curr = next(d for d in analysis_data if d['label'] == sel_label)

        st.markdown(f"### 🏟️ {sel_label}")
        c1, c2, c3 = st.columns(3)
        c1.metric(curr['h_ch'], f"{curr['final_p_h']:.1f}%", f"預測分差: {curr['final_m_h']:+.1f}")
        c2.metric(curr['a_ch'], f"{100-curr['final_p_h']:.1f}%", f"預測分差: {-curr['final_m_h']:+.1f}")
        c3.metric("AI 建議贏家", curr['h_ch'] if curr['final_p_h'] > 50 else curr['a_ch'])

        # 🚑 傷病（交叉比對）
        ic1, ic2 = st.columns(2)
        with ic1:
            st.write(f"**{curr['h_ch']} 傷病（ESPN + NBA官方）**")
            for d in curr['h_det']:
                st.write(d)
            if not curr['h_det']:
                st.success("目前健康 / 無顯著傷病影響")
        with ic2:
            st.write(f"**{curr['a_ch']} 傷病（ESPN + NBA官方）**")
            for d in curr['a_det']:
                st.write(d)
            if not curr['a_det']:
                st.success("目前健康 / 無顯著傷病影響")

        # 📊 團隊深度數據（你原本 v6.9 表格）
        def get_m(m, tid, k): 
            return maps.get(m, {}).get(int(tid), {}).get(k, 0)

        st.subheader("📊 團隊深度數據對比")
        st.table(pd.DataFrame({
            "指標項目": ["進攻效率", "防守效率", "節奏", "轉換得分 (PPP)", "跑動(mi)", "場均傳球", "干擾投籃", "撥球"],
            curr['h_ch']: [
                get_m('adv', curr['h_id'], 'OFF_RATING'),
                get_m('adv', curr['h_id'], 'DEF_RATING'),
                get_m('adv', curr['h_id'], 'PACE'),
                get_m('trans', curr['h_id'], 'PPP'),
                get_m('spd', curr['h_id'], 'DIST_MILES'),
                get_m('pass', curr['h_id'], 'PASSES_MADE'),
                get_m('hustle', curr['h_id'], 'CONTESTED_SHOTS'),
                get_m('hustle', curr['h_id'], 'DEFLECTIONS')
            ],
            curr['a_ch']: [
                get_m('adv', curr['a_id'], 'OFF_RATING'),
                get_m('adv', curr['a_id'], 'DEF_RATING'),
                get_m('adv', curr['a_id'], 'PACE'),
                get_m('trans', curr['a_id'], 'PPP'),
                get_m('spd', curr['a_id'], 'DIST_MILES'),
                get_m('pass', curr['a_id'], 'PASSES_MADE'),
                get_m('hustle', curr['a_id'], 'CONTESTED_SHOTS'),
                get_m('hustle', curr['a_id'], 'DEFLECTIONS')
            ]
        }))

        # 🚀 核心球員 Top 6（排除 NBA 官方 Inactive）
        st.subheader("🚀 核心球員名單（Top 6；排除 NBA 官方 Inactive）")

        # 取得該隊 NBA官方 inactive 名單（用 name normalize 對齊）
        nba_inactives_norm = {
            abbr: set(normalize_name(p['name']) for p in plist)
            for abbr, plist in (nba_inactive_report or {}).items()
        }

        p1, p2 = st.columns(2)
        for tid, abbr, name, col in [(curr['h_id'], curr['h_abbr'], curr['h_ch'], p1),
                                     (curr['a_id'], curr['a_abbr'], curr['a_ch'], p2)]:
            with col:
                if ps_full is None or ps_full.empty:
                    st.info("球員資料目前抓不到（LeagueDashPlayerStats empty）")
                    continue

                p_df = ps_full[ps_full['TEAM_ID'] == tid].copy()
                if not p_df.empty:
                    # 排除官方 inactive
                    inactive_set = nba_inactives_norm.get(abbr, set())
                    if inactive_set:
                        p_df['__nm'] = p_df['PLAYER_NAME'].apply(normalize_name)
                        p_df = p_df[~p_df['__nm'].isin(inactive_set)].drop(columns=['__nm'])

                    p_df = p_df.sort_values('PTS', ascending=False).head(6)

                st.write(f"**{name}**")
                st.dataframe(
                    p_df[['PLAYER_NAME', 'PTS', 'REB', 'AST', 'TS_PCT']]
                    .rename(columns={'PLAYER_NAME':'姓名','PTS':'得分','TS_PCT':'真實命中%'}),
                    hide_index=True
                )

st.sidebar.info(f"🕒 系統更新：{last_update}")
