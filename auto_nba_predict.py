import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats, 
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    synergyplaytypes, leaguedashptdefend
)
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import pytz, warnings
from datetime import datetime, timedelta

# --- 1. 基本設定 ---
warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')

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

st.set_page_config(page_title="NBA 數據專家 v7.2", layout="wide")
st.title("🏀 NBA 數據專家 v7.2 (穩定性修復)")

# --- 2. 強化版安全抓取 ---
def fetch_safe_df(endpoint_class, **kwargs):
    try:
        instance = endpoint_class(**kwargs)
        df = instance.get_data_frames()[0]
        id_col = next((c for c in df.columns if 'ID' in c.upper()), None)
        if id_col: df['TEAM_ID'] = df[id_col].astype(int)
        return df
    except: return pd.DataFrame()

@st.cache_data(ttl=3600)
def load_all_data_v72():
    S = '2025-26'
    ST = 'Regular Season'
    nba_ids = [t['id'] for t in teams.get_teams()]
    
    # [數據抓取]
    df_base = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, per_mode_detailed='PerGame')
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense='Advanced')
    df_hustle = fetch_safe_df(leaguehustlestatsteam.LeagueHustleStatsTeam, season=S, per_mode_time='PerGame')
    df_spd = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='SpeedDistance', per_mode_simple='PerGame')
    df_pass = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type='Passing', per_mode_simple='PerGame')
    
    # 戰術數據修復：如果 2025-26 為空，嘗試抓取數據
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Transition', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
    df_iso = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable='Isolation', player_or_team_abbreviation='T', season=S, season_type_all_star=ST)
    df_rim = fetch_safe_df(leaguedashptdefend.LeagueDashPtDefend, season=S, defense_category='Less Than 6 Ft', season_type_all_star=ST)

    def to_map(df, cols):
        return df.set_index('TEAM_ID')[cols].to_dict('index') if not df.empty and 'TEAM_ID' in df.columns else {}

    maps = {
        'base': to_map(df_base, ['PTS', 'REB', 'AST', 'FG_PCT']),
        'adv': to_map(df_adv, ['OFF_RATING', 'DEF_RATING', 'PACE']),
        'hustle': to_map(df_hustle, ['DEFLECTIONS', 'CONTESTED_SHOTS']),
        'spd': to_map(df_spd, ['DIST_MILES', 'AVG_SPEED']),
        'pass': to_map(df_pass, ['PASSES_MADE']),
        'trans': to_map(df_trans, ['PPP']),
        'iso': to_map(df_iso, ['PPP']),
        'rim': to_map(df_rim, ['D_FG_PCT'])
    }

    # --- 修復 AttributeError: .dt accessor ---
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw['TEAM_ID'].isin(nba_ids)].copy()
    
    # 關鍵：強制轉換日期格式
    gf['GAME_DATE'] = pd.to_datetime(gf['GAME_DATE'], errors='coerce')
    gf = gf.dropna(subset=['GAME_DATE']) # 剔除日期無效的欄位
    
    gf['WIN_BIN'] = gf['WL'].apply(lambda x: 1 if x == 'W' else 0)
    gf['IS_HOME'] = gf['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    gf = gf.sort_values(['TEAM_ID', 'GAME_DATE'])
    
    # 現在可以使用 .dt 了
    gf['REST_DAYS'] = gf.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days.fillna(3)

    def gv(tid, m, k, d=0): return maps[m].get(int(tid), {}).get(k, d)

    feats = ['IS_HOME', 'REST_DAYS', 'F_PTS', 'F_REB', 'F_AST', 'F_ORTG', 'F_DRTG', 'F_PACE', 
             'F_DEFL', 'F_CONT', 'F_DIST', 'F_SPD', 'F_PASS', 'F_TRANS', 'F_ISO', 'F_RIM']
    
    gf['F_PTS'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'base', 'PTS', 110))
    gf['F_REB'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'base', 'REB', 44))
    gf['F_AST'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'base', 'AST', 25))
    gf['F_ORTG'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'adv', 'OFF_RATING', 110))
    gf['F_DRTG'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'adv', 'DEF_RATING', 110))
    gf['F_PACE'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'adv', 'PACE', 99))
    gf['F_DEFL'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'hustle', 'DEFLECTIONS', 15))
    gf['F_CONT'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'hustle', 'CONTESTED_SHOTS', 40))
    gf['F_DIST'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'spd', 'DIST_MILES', 18))
    gf['F_SPD'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'spd', 'AVG_SPEED', 4.4))
    gf['F_PASS'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'pass', 'PASSES_MADE', 280))
    gf['F_TRANS'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'trans', 'PPP', 1.1))
    gf['F_ISO'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'iso', 'PPP', 0.9))
    gf['F_RIM'] = gf['TEAM_ID'].apply(lambda x: gv(x, 'rim', 'D_FG_PCT', 0.6))

    # 模型純化
    train_x = gf[feats].apply(pd.to_numeric, errors='coerce').fillna(0)
    clf = xgb.XGBClassifier(n_estimators=100).fit(train_x, gf['WIN_BIN'])
    reg = xgb.XGBRegressor(n_estimators=100).fit(train_x, gf['PLUS_MINUS'].fillna(0))
    
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame')
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed='PerGame', measure_type_detailed_defense='Advanced')
    ps_full = pd.merge(ps_raw[['PLAYER_ID', 'TEAM_ID', 'PLAYER_NAME', 'PTS', 'REB', 'AST']], ps_adv[['PLAYER_ID', 'TS_PCT', 'PIE']], on='PLAYER_ID')
    
    return clf, reg, gf, ps_full, feats, maps, datetime.now(tw_tz).strftime("%H:%M")

clf, reg, gf, ps_full, feats, maps, last_update = load_all_data_v72()

# --- 3. 介面呈現 ---
dates = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)]
tabs = st.tabs([d.strftime('%m/%d') for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=dates[i].strftime('%m/%d/%Y'))
        if sb.empty: st.info("📅 今日無賽程")
        else:
            id_to_abbr = {t['id']: t['abbreviation'] for t in teams.get_teams()}
            games = {}
            for _, row in sb.iterrows():
                h_id, a_id = row['HOME_TEAM_ID'], row['VISITOR_TEAM_ID']
                h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
                if h_abbr and a_abbr:
                    h_last = gf[gf['TEAM_ABBREVIATION'] == h_abbr].tail(1)
                    if not h_last.empty:
                        test_x = h_last[feats].apply(pd.to_numeric, errors='coerce').fillna(0)
                        prob = clf.predict_proba(test_x)[0][1] * 100
                        diff = round(abs(float(reg.predict(test_x)[0])), 1)
                        games[f"{TEAM_NAME_CH.get(a_abbr)} @ {TEAM_NAME_CH.get(h_abbr)}"] = {
                            'h_id': h_id, 'a_id': a_id, 'h_name': TEAM_NAME_CH.get(h_abbr), 'a_name': TEAM_NAME_CH.get(a_abbr),
                            'prob': prob, 'diff': diff, 'winner': TEAM_NAME_CH.get(h_abbr if prob > 50 else a_abbr)
                        }

            if games:
                selected = st.selectbox("🎯 選擇分析場次", list(games.keys()), key=f"s_{i}")
                res = games[selected]
                st.write(f"### 🏟️ {selected}")
                c1, c2, c3 = st.columns(3)
                c1.metric(res['h_name'], f"{res['prob']:.1f}%")
                c2.metric(res['a_name'], f"{100-res['prob']:.1f}%")
                c3.metric("AI 預測贏家", res['winner'], f"分差預估: {res['diff']} 分")

                def gm(m, tid, k): return maps[m].get(int(tid), {}).get(k, 0)

                # 表格美化與單位
                st.subheader("📊 1. 團隊場均數據 (全指標)")
                st.table(pd.DataFrame({
                    "指標項目": ["得分", "籃板", "進攻效率", "防守效率", "撥球破壞", "跑動里程"],
                    res['h_name']: [f"{gm('base',res['h_id'],'PTS'):.1f} 分", f"{gm('base',res['h_id'],'REB'):.1f} 個", f"{gm('adv',res['h_id'],'OFF_RATING')} pts", f"{gm('adv',res['h_id'],'DEF_RATING')} pts", f"{gm('hustle',res['h_id'],'DEFLECTIONS'):.1f} 次", f"{gm('spd',res['h_id'],'DIST_MILES'):.2f} mi"],
                    res['a_name']: [f"{gm('base',res['a_id'],'PTS'):.1f} 分", f"{gm('base',res['a_id'],'REB'):.1f} 個", f"{gm('adv',res['a_id'],'OFF_RATING')} pts", f"{gm('adv',res['a_id'],'DEF_RATING')} pts", f"{gm('hustle',res['a_id'],'DEFLECTIONS'):.1f} 次", f"{gm('spd',res['a_id'],'DIST_MILES'):.2f} mi"]
                }))

                st.subheader("⚔️ 2. 戰術與護框 (模型關鍵特徵)")
                st.table(pd.DataFrame({
                    "戰術指標": ["轉換進攻 PPP", "單打 PPP", "護框命中率 %"],
                    res['h_name']: [f"{gm('trans',res['h_id'],'PPP'):.2f}", f"{gm('iso',res['h_id'],'PPP'):.2f}", f"{gm('rim',res['h_id'],'D_FG_PCT'):.1%}"],
                    res['a_name']: [f"{gm('trans',res['a_id'],'PPP'):.2f}", f"{gm('iso',res['a_id'],'PPP'):.2f}", f"{gm('rim',res['a_id'],'D_FG_PCT'):.1%}"]
                }))

st.sidebar.caption(f"🕒 更新時間：{last_update}")
