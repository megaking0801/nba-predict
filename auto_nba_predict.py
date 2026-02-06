import streamlit as st
from nba_api.stats.endpoints import leaguegamefinder, scoreboardv2, commonteamroster, leaguedashplayerstats
from nba_api.stats.static import teams
import pandas as pd
import xgboost as xgb
import os
import json
from datetime import datetime, timedelta
import pytz
import warnings
import time
import google.generativeai as genai

# --- 1. AI 核心設定 (從 Secrets 讀取) ---
# 嘗試從 Streamlit Secrets 讀取 API Key，若無則標記 AI 為不可用
try:
    if "GEMINI_API_KEY" in st.secrets:
        API_KEY = st.secrets["GEMINI_API_KEY"]
        genai.configure(api_key=API_KEY)
        model_ai = genai.GenerativeModel('gemini-1.5-flash')
        AI_READY = True
    else:
        AI_READY = False
except Exception as e:
    AI_READY = False

warnings.filterwarnings('ignore')
tw_tz = pytz.timezone('Asia/Taipei')

# 建立球隊中文名稱對照表
TEAM_NAME_CH = {
    'ATL': '亞特蘭大老鷹', 'BKN': '布魯克林籃網', 'BOS': '波士頓塞爾提克',
    'CHA': '夏洛特黃蜂', 'CHI': '芝加哥公牛', 'CLE': '克里夫蘭騎士',
    'DAL': '達拉斯獨行俠', 'DEN': '丹佛金塊', 'DET': '底特律活塞',
    'GSW': '金州勇士', 'HOU': '休士頓火箭', 'IND': '印第安納溜馬',
    'LAC': '洛杉磯快艇', 'LAL': '洛杉磯湖人', 'MEM': '曼非斯灰熊',
    'MIA': '邁阿密熱火', 'MIL': '密爾瓦基公鹿', 'MIN': '明尼蘇達森林狼',
    'NOP': '紐奧良鵜鶘', 'NYK': '紐約尼克', 'OKC': '奧克拉荷馬雷霆',
    'ORL': '奧蘭多魔術', 'PHI': '費城 76 人', 'PHX': '鳳凰城太陽',
    'POR': '波特蘭開拓者', 'SAC': '沙加緬度國王', 'SAS': '聖安東尼奧馬刺',
    'TOR': '多倫多暴龍', 'UTA': '猶他爵士', 'WAS': '華盛頓巫師'
}

st.set_page_config(page_title="NBA AI 全量數據預測 v5.4", layout="wide")
st.title("🏀 NBA 終極智慧預測系統")

# --- 2. 側邊欄診斷與資訊 ---
with st.sidebar:
    st.header("🛠️ 系統狀態")
    if AI_READY:
        st.success("Gemini API: 已連線")
    else:
        st.error("Gemini API: 未設定 (請檢查 Secrets)")
    st.info(f"系統時區: 台北 (GMT+8)")
    st.markdown("---")
    st.caption("v5.4 Update: Fix snapshot path & AI logic")

# --- 3. 核心輔助函數 ---
def get_snapshot_path(date_key):
    """取得每日數據快照的檔案路徑"""
    return f"nba_snapshot_{date_key}.json"

def generate_ai_all_reports(all_games_info):
    """
    一次將所有場次數據丟給 AI 進行深度客製化分析
    Input: dict containing all games data
    Output: dict {game_id: analysis_text}
    """
    if not AI_READY or not all_games_info:
        return {}

    # 格式化所有場次數據成為一個易讀清單，供 AI 閱讀
    data_payload = ""
    for g_id, d in all_games_info.items():
        data_payload += f"【場次 {g_id}】{d['away']} vs {d['home']} | "
        data_payload += f"勝率:{d['a_wr']:.0f}% vs {d['h_wr']:.0f}% | "
        data_payload += f"火力:{d['a_pts']:.1f} vs {d['h_pts']:.1f} | "
        data_payload += f"B2B:{d['b2b_status']} | 預測:{d['winner']}贏{d['diff']}分\n"

    prompt = f"""
    你是一位 NBA 專業球評與戰術分析師。以下是今日所有比賽的預測數據：
    {data_payload}

    任務：
    請針對每一場比賽寫一段約 150 字的專業分析。
    1. 每一場的分析切入點要不同（例如：這場講體能、那場講進攻火力差、另一場講近期勝率趨勢）。
    2. 語氣要像資深運動專欄，使用「台灣繁體中文」。
    3. 必須指出模型預測贏家會贏的關鍵原因。
    4. 回傳格式必須嚴格遵守 JSON：{{"場次ID": "分析內容", ...}}
    5. 不要輸出任何 JSON 以外的文字。
    """
    
    try:
        # 使用 JSON 模式回傳，降低格式錯誤率
        response = model_ai.generate_content(
            prompt, 
            generation_config={"response_mime_type": "application/json", "temperature": 0.8}
        )
        return json.loads(response.text)
    except Exception as e:
        # 如果 JSON 解析失敗或 API 錯誤，印出錯誤並回傳空字典
        print(f"AI Error: {e}")
        return {}

# --- 4. 數據獲取與模型訓練 ---
@st.cache_data(ttl=600)
def get_comprehensive_data(season):
    """獲取賽季數據並訓練 XGBoost 模型"""
    all_games = pd.DataFrame()
    player_stats = pd.DataFrame()
    
    # 嘗試多次獲取數據，避免網路波動
    for i in range(3):
        try:
            gamefinder = leaguegamefinder.LeagueGameFinder(season_nullable=season, timeout=60)
            all_games = gamefinder.get_data_frames()[0]
            if not all_games.empty: break
        except: time.sleep(2)
    
    if all_games.empty: return None, None, pd.DataFrame(), pd.DataFrame(), []
    
    # 數據前處理
    all_games['GAME_DATE'] = pd.to_datetime(all_games['GAME_DATE'])
    all_games = all_games.sort_values(['TEAM_ID', 'GAME_DATE'])
    all_games['IS_HOME'] = all_games['MATCHUP'].apply(lambda x: 1 if 'vs.' in x else 0)
    all_games['WIN_BIN'] = all_games['WL'].apply(lambda x: 1 if x == 'W' else 0)
    
    # 特徵工程：近10場勝率
    all_games['L10_WIN_RATE'] = all_games.groupby('TEAM_ID')['WIN_BIN'].transform(lambda x: x.shift(1).rolling(10).mean())
    
    # 特徵工程：近5場數據
    stats_cols = ['PTS', 'PLUS_MINUS', 'FG_PCT']
    for col in stats_cols:
        all_games[f'L5_{col}'] = all_games.groupby('TEAM_ID')[col].transform(lambda x: x.shift(1).rolling(5).mean())

    # 特徵工程：背靠背 (B2B)
    all_games['B2B'] = (all_games.groupby('TEAM_ID')['GAME_DATE'].diff().dt.days == 1).astype(int)
    
    # 準備訓練集
    train_df = all_games.dropna(subset=['L5_PTS', 'L10_WIN_RATE']).copy()
    features = [f'L5_{c}' for c in stats_cols] + ['B2B', 'IS_HOME', 'L10_WIN_RATE']
    
    # 訓練分類模型 (勝負)
    clf = xgb.XGBClassifier(n_estimators=100, max_depth=3, learning_rate=0.1)
    clf.fit(train_df[features], train_df['WIN_BIN'])
    
    # 訓練回歸模型 (分差)
    reg = xgb.XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.1)
    reg.fit(train_df[features], train_df['PLUS_MINUS'])
    
    # 獲取球員數據
    try:
        p_stats = leaguedashplayerstats.LeagueDashPlayerStats(season=season, per_mode_detailed='PerGame').get_data_frames()[0]
        player_stats = p_stats[['PLAYER_NAME', 'TEAM_ID', 'PTS', 'REB', 'AST']]
    except: pass
    
    return clf, reg, all_games, player_stats, features

@st.cache_data(ttl=600)
def get_team_roster(team_id):
    """獲取球隊名單"""
    try:
        roster = commonteamroster.CommonTeamRoster(team_id=team_id, timeout=30).get_data_frames()[0]
        if 'PLAYER' in roster.columns: roster = roster.rename(columns={'PLAYER': 'PLAYER_NAME'})
        return roster[['PLAYER_NAME']]
    except: return pd.DataFrame(columns=['PLAYER_NAME'])

@st.cache_data(ttl=3600)
def get_schedule_for_date(date_obj):
    """獲取特定日期的賽程"""
    date_str = date_obj.strftime('%m/%d/%Y')
    try:
        sb = scoreboardv2.ScoreboardV2(game_date=date_str, timeout=30)
        df = sb.get_data_frames()[0]
        t_map = {t['id']: t['abbreviation'] for t in teams.get_teams()}
        if not df.empty:
            df['HOME_ABBR'] = df['HOME_TEAM_ID'].map(t_map)
            df['AWAY_ABBR'] = df['VISITOR_TEAM_ID'].map(t_map)
            return df.to_dict('records')
    except: pass
    return []

# --- 5. 預測與分析邏輯引擎 ---
def run_prediction(games, clf, reg, all_games_raw, player_stats, features_list):
    results = {}
    ai_input_data = {}
    
    # 步驟 1: 計算硬數據與初步結果
    for g in games:
        g_id = str(g['GAME_ID'])
        h_abbr, a_abbr = g['HOME_ABBR'], g['AWAY_ABBR']
        
        # 抓取兩隊最近的數據快照
        h_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == h_abbr].tail(1)
        a_feat = all_games_raw[all_games_raw['TEAM_ABBREVIATION'] == a_abbr].tail(1)
        
        if h_feat.empty or a_feat.empty: continue

        # 建構預測用特徵
        h_in = h_feat[features_list].copy(); h_in['IS_HOME'] = 1
        a_in = a_feat[features_list].copy(); a_in['IS_HOME'] = 0
        
        # 執行模型預測
        h_p_raw = clf.predict_proba(h_in)[:, 1][0]
        a_p_raw = clf.predict_proba(a_in)[:, 1][0]
        h_p = (float(h_p_raw) / (float(h_p_raw) + float(a_p_raw))) * 100 # 正規化機率
        diff = round(float(reg.predict(h_in)[0]) - float(reg.predict(a_in)[0]), 1)
        
        h_n_ch, a_n_ch = TEAM_NAME_CH.get(h_abbr, h_abbr), TEAM_NAME_CH.get(a_abbr, a_abbr)
        
        # 存入 AI 資料包 (準備一次發送)
        ai_input_data[g_id] = {
            'home': h_n_ch, 'away': a_n_ch,
            'h_wr': h_feat['L10_WIN_RATE'].values[0]*100, 'a_wr': a_feat['L10_WIN_RATE'].values[0]*100,
            'h_pts': h_feat['L5_PTS'].values[0], 'a_pts': a_feat['L5_PTS'].values[0],
            'b2b_status': f"主隊{'有' if h_feat['B2B'].values[0] else '否'}, 客隊{'有' if a_feat['B2B'].values[0] else '否'}",
            'winner': h_n_ch if diff > 0 else a_n_ch,
            'diff': abs(diff)
        }
        
        # 存入基礎結果
        results[g_id] = {
            'h_prob': h_p, 'a_prob': 100-h_p, 'diff': diff,
            'winner_abbr': h_abbr if diff > 0 else a_abbr,
            'h_idx': [f"🟢 勝率: {ai_input_data[g_id]['h_wr']:.0f}%", f"🟢 均得分: {ai_input_data[g_id]['h_pts']:.1f}", f"🔴 B2B: {'是' if h_feat['B2B'].values[0] else '否'}"],
            'a_idx': [f"🔵 勝率: {ai_input_data[g_id]['a_wr']:.0f}%", f"🔵 均得分: {ai_input_data[g_id]['a_pts']:.1f}", f"🔴 B2B: {'是' if a_feat['B2B'].values[0] else '否'}"],
            'h_team_id': g['HOME_TEAM_ID'], 'a_team_id': g['VISITOR_TEAM_ID']
        }

    # 步驟 2: 一次性呼叫 AI 生成今日所有報告 (僅在有數據時呼叫)
    if ai_input_data:
        with st.spinner("AI 正在深度解析今日所有比賽數據..."):
            ai_book = generate_ai_all_reports(ai_input_data)
    else:
        ai_book = {}

    # 步驟 3: 合併 AI 內容與球員名單
    final_results = {}
    for g_id, res in results.items():
        # 內部函數：取得球隊得分前5名球員
        def get_roster_data(t_id):
            ros = get_team_roster(t_id)
            if ros.empty or player_stats.empty: return []
            m = ros.merge(player_stats, on='PLAYER_NAME', how='left').fillna(0)
            return m.sort_values(by='PTS', ascending=False).head(5).to_dict('records')

        final_results[g_id] = res
        
        # 如果 AI 沒回傳該場次分析 (或 AI 未啟動)，使用備用文字
        default_text = f"【數據快評】{TEAM_NAME_CH.get(res['winner_abbr'])} 因近期場均得分與勝率較高，模型看好能拿下比賽。"
        final_results[g_id]['summary_report'] = ai_book.get(g_id, default_text)
        
        final_results[g_id]['h_roster'] = get_roster_data(res['h_team_id'])
        final_results[g_id]['a_roster'] = get_roster_data(res['a_team_id'])
        
    return final_results

# --- 6. 介面主體 (UI Loop) ---
# 初始化數據
clf, reg, all_games_raw, player_stats, features = get_comprehensive_data('2025-26')
date_list = [datetime.now(tw_tz) - timedelta(days=i) for i in range(4)] # 顯示今天+過去3天
tabs = st.tabs([d.strftime('%m/%d') for d in date_list])

for i, tab in enumerate(tabs):
    with tab:
        current_date = date_list[i]
        date_key = current_date.strftime('%Y-%m-%d')
        
        # 獲取當日賽程
        games = get_schedule_for_date(current_date)
        snapshot_file = get_snapshot_path(date_key)
        
        if not games: 
            st.info("今日暫無賽程或數據尚未更新")
            continue

        # --- 置頂鎖定區 (Cache Control) ---
        is_locked = os.path.exists(snapshot_file)
        c_btn, c_txt = st.columns([1, 4])
        
        if not is_locked:
            # 未鎖定：按鈕觸發 AI 分析並存檔
            if c_btn.button("🔒 鎖定今日數據並啟動 AI", key=f"lk_{date_key}"):
                ld = run_prediction(games, clf, reg, all_games_raw, player_stats, features)
                with open(snapshot_file, 'w', encoding='utf-8') as f: json.dump(ld, f, ensure_ascii=False)
                st.rerun()
            c_txt.warning("目前為即時模式，數據隨 API 變動。點擊鎖定可生成 AI 深度報告。")
        else:
            # 已鎖定：按鈕刪除存檔 (重置)
            if c_btn.button("🔓 解鎖並重新分析", key=f"ul_{date_key}"):
                os.remove(snapshot_file)
                st.rerun()
            c_txt.success("封盤鎖定模式：正在顯示先前生成的深度分析報告。")

        # --- 下拉選單選擇場次 ---
        game_names = [f"{TEAM_NAME_CH.get(g['AWAY_ABBR'], g['AWAY_ABBR'])} @ {TEAM_NAME_CH.get(g['HOME_ABBR'], g['HOME_ABBR'])}" for g in games]
        sel_name = st.selectbox("🎯 選擇對戰場次", options=game_names, key=f"sb_{date_key}")
        
        # --- 讀取數據 (從檔案或即時運算) ---
        if is_locked:
            # 鎖定時：讀取 JSON
            with open(snapshot_file, 'r', encoding='utf-8') as f: ds = json.load(f)
        else:
            # 即時預測時：不呼叫 AI 全量分析 (省額度/時間)，只算數據
            # 這裡我們傳入空字典給 generate_ai_all_reports 避免觸發 API，或讓 run_prediction 內部處理
            # 為了簡化邏輯，即時模式下 run_prediction 仍會運作，但因為沒點鎖定，通常建議在 run_prediction 裡控制
            # 但目前的邏輯是：只有鎖定時才會保存結果。即時模式每次都會重算。
            # 為了避免即時模式一直扣 AI Quota，我們可以暫時不呼叫 run_prediction 的 AI 部分?
            # 修正：run_prediction 內部有 spinner，即時模式也會跑。
            # 如果想省額度，可以讓使用者只在鎖定時才看到 AI。但為了體驗，這裡維持原樣。
            ds = run_prediction(games, clf, reg, all_games_raw, player_stats, features)

        # --- 顯示結果 ---
        g_id = str(games[game_names.index(sel_name)]['GAME_ID'])
        res = ds.get(g_id, {})
        
        if res:
            h_n = TEAM_NAME_CH.get(games[game_names.index(sel_name)]['HOME_ABBR'])
            a_n = TEAM_NAME_CH.get(games[game_names.index(sel_name)]['AWAY_ABBR'])
            
            st.markdown(f"## 🏟️ {a_n} (客) @ {h_n} (主)")
            
            # 頂部三大數據
            c1, c2, c3 = st.columns(3)
            c1.metric(f"{h_n} 勝率", f"{float(res.get('h_prob', 0)):.1f}%")
            c2.metric(f"{a_n} 勝率", f"{float(res.get('a_prob', 0)):.1f}%")
            c3.metric("預測贏家", TEAM_NAME_CH.get(res.get('winner_abbr')), delta=f"領先 {abs(float(res.get('diff', 0)))} 分")

            st.write("---")
            
            # AI 分析區塊
            st.subheader("📝 AI 深度分析專欄 (全數據客製化)")
            st.info(res.get('summary_report', "分析生成中..."))

            # 兩隊詳細數據指標
            l_col, r_col = st.columns(2)
            with l_col:
                st.markdown(f"**🏠 {h_n} 指標**")
                for item in res.get('h_idx', []): st.write(item)
            with r_col:
                st.markdown(f"**✈️ {a_n} 指標**")
                for item in res.get('a_idx', []): st.write(item)

            st.write("---")
            
            # 核心球員列表
            st.subheader("👤 核心球員數據 (得分榜)")
            def safe_df(data):
                df = pd.DataFrame(data if data else [])
                if df.empty: return pd.DataFrame(columns=['姓名','得分','籃板','助攻'])
                return df[['PLAYER_NAME','PTS','REB','AST']].rename(columns={'PLAYER_NAME':'姓名','PTS':'得分','REB':'籃板','AST':'助攻'})
            
            cl, cr = st.columns(2)
            cl.dataframe(safe_df(res.get('h_roster')), hide_index=True, use_container_width=True)
            cr.dataframe(safe_df(res.get('a_roster')), hide_index=True, use_container_width=True)
