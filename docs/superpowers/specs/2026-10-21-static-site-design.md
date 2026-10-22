# 靜態網站（一直在線）設計規格

日期：2026-10-21
狀態：已納入支線審查 14 項意見（2026-10-23）。使用者授權兩個 session 自行決定細節；push、啟用 Pages、觸發 workflow、寫入正式 DB 仍須使用者明確同意。

## 1. 目的與背景

目標：網站隨時打開網址就能用，不再出現「睡眠頁」，且排程失效時頁面只是資料變舊、不會壞掉。

目前的網站是 Streamlit 應用，部署在 Streamlit Community Cloud。調查結果：

- Community Cloud 沒流量 12 小時就讓 app 睡著，訪客要按「Yes, get this app back up!」才會醒。官方沒有常駐選項；單純的 HTTP ping 不會喚醒（要瀏覽器執行 JS 並連上 WebSocket）。
  來源：[Streamlit 文件](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)、[DEV 實測](https://dev.to/yasumorishima/http-200-but-your-streamlit-app-is-still-sleeping-why-get-requests-dont-work-and-how-playwright-3g67)
- `daily_nba` 排程狀態為 `disabled_inactivity`，最後一次排程執行為 2026-08-19。公開 repo 60 天沒活動，排程會被自動停用。
  來源：[GitHub 文件](https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-workflow-runs/disabling-and-enabling-a-workflow)
- 最近 40 次執行有 21 次失敗：10:30 UTC 全流程中，`ingest_games` 被 `cdn.nba.com` 回 403，`ingest_stats` 打 `stats.nba.com` 逾時。NBA 官方端點擋了 GitHub runner。

站台本身幾乎是唯讀、資料一天才更新幾次，所以最適合的根治做法是**靜態網站**：每次排程結束後從 DB 產生 HTML，放在 GitHub Pages。

## 2. 已做的決定

| 項目 | 決定 | 理由 |
| :- | :- | :- |
| 託管 | GitHub Pages（Actions 部署） | 不需新帳號；repo 為公開，符合免費條件。 |
| 第一版範圍 | 與 Streamlit 完全對等 | 看板、單場詳情、30 個球隊頁、模型戰績。 |
| 實作方式 | 抽出共用渲染模組，Python 直接產生 HTML | 設計只有一份；不引入新套件或工具鏈。 |
| Streamlit | 暫時保留，行為不變 | 退役與否另議。 |

被排除的做法：讓瀏覽器直接查 Supabase（要重寫成 JS 並開放 DB 權限）；stlite（首次載入太重，且瀏覽器連不到 psycopg2）；靜態站台框架（工具鏈與工作量過大）；Playwright 保活（非官方、仍有空窗）；付費常駐主機（需持續付費）。

## 3. 範圍外

- 恢復 `daily_nba` 排程、讓抓資料改走 ESPN（`ingest_espn.py` 已存在但未接進 `run_daily`）。這是**第二個子專案**，緊接著做，否則網站內容會停在舊資料。本專案先用現有 DB 資料建置與驗證。
- 退役 Streamlit、自訂網域、唯讀 DB 帳號（可選的加強，預設先用現有 secrets）。

## 4. 架構

### 4.1 元件

| 檔案 | 角色 |
| :- | :- |
| `webui/render.py`（新） | 純函式：資料列轉 HTML。包含 `NEON_CSS`、`DISCLAIMER_HTML`、傷病中文對照、卡片、勝率條、三種玩法卡、傷病面板、球員表、頁面組裝。不 import `streamlit`。 |
| `webui/queries.py`（新） | 全部 SQL，函式簽名接收一個 `run(sql, params) -> DataFrame` 的 callable。只含 `SELECT`。 |
| `webui/build_site.py`（新） | 開一個 DB 連線（`jobs.db_utils.db_connect`），讀資料、渲染、輸出 `site/`。 |
| `auto_nba_predict.py`（改薄） | 只保留 Streamlit 外殼與路由；import 上面兩個模組；`queries` 的函式以 `st.cache_data` 包裝。行為與輸出不變。 |
| `.github/workflows/site.yml`（新） | 建置與部署。 |
| `tests/test_webui_*.py`（新） | 見第 8 節。 |

補充規則（審查後）：
- `render.py` 的頁面組裝函式一律接收 `now` 參數，不自己呼叫 `datetime.now`；`injury_panel` 改為接收傷病 DataFrame，不自己查詢。渲染與查詢完全分離。
- CSS 不拆：`NEON_CSS` 原樣共用（拆開會動到 Streamlit 版輸出）。靜態頁用 `<div class="stApp"><div class="block-container">` 包起來，讓既有選擇器直接生效，另加 `STATIC_EXTRA_CSS`（body 邊距、`<details>` 樣式）；`st.info` 空狀態與 `st.expander` 模型說明在靜態版改為自己的 HTML（`<details>`）。這些是「靜態版等價物」，不在逐字 golden 範圍內。
- `auto_nba_predict.py` 保留各函式自己的薄包裝與原本的 TTL，例如 `@st.cache_data(ttl=300) def load_recent_games(n=30): return queries.recent_games(q, n)`；不把 `run` callable 傳進 `st.cache_data`。
- `build_site` 自己設 `connect_timeout` 與 `statement_timeout`，不改共用的 `jobs.db_utils.db_connect`；連線後 `set_session(readonly=True)`。
- 寫檔名前驗證：`game_id` 須符合 `re.fullmatch(r"[0-9A-Za-z_-]+")`，不合格讓 build 失敗；隊名縮寫一律走 `require_abbr`。

`webui/` 同時需要 `__init__.py`，讓 `python -m webui.build_site` 可執行。`site/` 為建置輸出，加入 `.gitignore`，不提交。

### 4.2 連結抽象

`render.py` 的函式不寫死連結，而是接收一個 `links` 物件，提供 `home()`、`game(game_id)`、`team(abbr)`，回傳相對於「目前頁面」的 URL。
- Streamlit：`home()` 為 `?`，`game(id)` 為 `?game=<id>`，`team()` 不使用（保留下拉選單）。
- 靜態站：由 `build_site` 依頁面深度產生相對路徑（`index.html` 在根；`game/<id>.html` 與 `team/<ABBR>.html` 以 `../` 回到根）。
全站不使用以 `/` 開頭的絕對路徑，因為網址帶有 `/nba-predict/` 子路徑。

### 4.3 輸出頁面

| 檔案 | 內容 |
| :- | :- |
| `index.html` | 頂欄、今日／近期看板、最有把握的一場、模型戰績（含大小分）、30 隊連結清單（取代下拉選單）、模型說明、免責聲明、更新時間。 |
| `game/<game_id>.html` | 與 `load_recent_games(30)` 相同查詢結果中的每一場（`p_home_win` 非空的最新 30 場），內容同現有詳情頁（勝負、讓分、大小分、兩隊傷病、兩隊球員場均）。 |
| `team/<ABBR>.html` × 30 | 近 10 場與球員場均（最新球季、平均分鐘 ≥ 10）。 |
| `404.html`、`.nojekyll` | 簡單的找不到頁面、關閉 Jekyll 處理。 |

時間顯示維持現狀：資料庫存 UTC，頁面顯示台北時間。

### 4.4 資料流

DB（只做 SELECT）→ `build_site.py` → `site/` → GitHub Pages。訪客瀏覽時不碰 DB；DB 帳密只存在 Actions secrets，且只給 `build` job。

## 5. 資料新鮮度的誠實揭露

靜態頁不會自己變新，所以：
- 每頁顯示「資料更新於 YYYY-MM-DD HH:MM（台北時間）· 頁面建置於 …」。「資料更新於」取 `SELECT max(predicted_at) FROM public.predictions`（`queries.data_freshness`），不是建置時間；查不到時只顯示「頁面建置於 …」，不宣稱資料時間。
- 頁面內嵌該資料時間（ISO 格式，秒精度；查不到時用建置時間）。一小段 inline JS 在**超過 36 小時**沒更新時，於頁面頂部顯示「資料可能已過期」警示。這樣即使 `daily_nba` 恢復排程但抓資料仍被擋、網站天天重建，過期提示也會出現。休賽季沒有新預測時提示也會出現，屬預期的誠實揭露。
- 「今晚 N 場」等依建置當下計算的文字，一律搭配上述更新時間。

## 6. 部署、觸發與失敗處理

### 6.1 觸發

1. `workflow_run`：`daily_nba` 完成（`types: [completed]`），**不論成功或失敗**都觸發。官方文件：「A workflow run is triggered regardless of the conclusion of the previous workflow.」不下載上游 artifact。
2. `workflow_dispatch`：手動。
3. `push` 到 `main`，且路徑包含 `webui/**`、`.github/workflows/site.yml`、`jobs/teams.py`、`jobs/model.py`、`jobs/db_utils.py`、`requirements.txt`、`tests/**`。

安全：`checkout` 一律用預設分支，不指定 ref；禁止 checkout `github.event.workflow_run.head_sha` 或 `head_branch`（避免 workflow_run 的 pwn-request 模式）。

注意：`daily_nba` 目前被停用，第 1 項要等第二個子專案恢復排程後才會動；在那之前依賴第 2、3 項。不另外加獨立排程（資料沒進來時，重建的內容也一樣；過期提示已處理誠實揭露）。

### 6.2 Jobs

- 頂層 `permissions: contents: read`；`pages: write`、`id-token: write` 只給 `deploy` job。
- `build`：`timeout-minutes: 15`。checkout → Python 3.11 → `pip install -r requirements.txt` → `python -m pytest tests -q` → `python -m webui.build_site` → `actions/upload-pages-artifact`（路徑 `site/`）。DB secrets **只放在 `build_site` 那一個 step 的 `env`**，不放 job 層，所以 `pip install` 與 `pytest` 拿不到。
- `deploy`：`needs: build`；`permissions: pages: write, id-token: write`；`environment: github-pages`；`actions/deploy-pages`。這個 job 不拿到 DB secrets。
- 併發群組 `pages`，不中斷進行中的部署。
- Action 版本在實作時依官方文件確認（文件目前列 `upload-pages-artifact@v4`、`deploy-pages@v4`）。

來源：[使用自訂 workflow 部署 Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)、[workflow 事件](https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows)。

### 6.3 失敗處理

- `build` 失敗（DB 連不上、查詢錯誤、測試失敗）→ `deploy` 不執行 → 預期線上維持上一版。官方文件未明說，**第一次部署後用故意失敗實測**。
- `build_site` 先輸出到暫存資料夾，檢查 `index.html` 存在且所有站內連結都指到存在的檔案，通過後才換成 `site/`，不放半成品。
- DB 為空或非賽季：輸出與現有 Streamlit 相同的「尚無預測」畫面，不視為錯誤。
- 某隊沒有資料：該隊頁顯示「尚無資料」。

### 6.4 限制

站台 1 GB 上限、部署逾時 10 分鐘；自訂 workflow 不受「每小時 10 次建置」限制（[文件](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)）。本站約 62 頁，遠低於上限。

## 7. 安全

- 唯讀的真正保證在連線層：`build_site` 連上後執行 `conn.set_session(readonly=True)`。`webui/queries.py` 只含 `SELECT`，另以測試掃描 SQL 字串作第二道防線，用 `\b` 字邊界比對 `INSERT`、`UPDATE`、`DELETE`、`DROP`、`ALTER`、`CREATE`（避免 `updated_at` 之類欄位名誤判）。
- 來自 DB 的文字（球員名、傷病說明與翻譯後字串、球隊名、`line_source`、`season`、縮寫）輸出前一律 HTML escape，全站統一用同一個 `esc()`（`html.escape`）。`table_html` 先逐格 `esc()` 再 `to_html(escape=False)`，不混用 pandas 的 escape。`NEON_CSS`、`DISCLAIMER_HTML` 與其他模板 HTML 不 escape。
- escape 會改變輸出（例如 `D'Angelo` 變 `D&#x27;Angelo`），屬刻意的行為變更：與「純搬動」分成兩個 commit（見第 9 節）。實作上 `table_html` 只 escape 字串格，golden fixture 只用一般字元，所以 golden 預期不變；escape 另以含 `'`、`&`、`<script>` 的單元測試驗證。若 golden 因 escape 而變動，要先查是否改到一般文字，不可直接更新 golden。
- `404.html` 是「無絕對路徑」規則的唯一例外：Pages 對不存在的路徑都回傳它且網址列不變，相對連結在深層網址會壞，所以它用 inline JS 從 `location.pathname` 第一段組出首頁網址；`check_site` 對它豁免。
- 建置與測試的檔案讀寫一律明確 `encoding="utf-8"`（本機預設 cp950）。
- `.streamlit/secrets.toml` 不讀、不印、不提交（沿用 `CLAUDE.md` 與 `settings.json` 的規則）。
- 開啟 Pages（把來源改為 GitHub Actions）是 repo 設定變更，**須使用者同意後才做**。

## 8. 測試

全部不需要 DB，使用固定的假資料。

1. **特徵化測試（在搬動程式碼之前先寫）**：`auto_nba_predict.py` 是模組層腳本（`:28` `set_page_config`、`:902` 載入戰績，import 即連 DB），無法直接 import 取函式。改用 `streamlit.testing.v1.AppTest.from_file("auto_nba_predict.py")`，monkeypatch `psycopg2.connect`（或假的 `q`），收集所有 `at.markdown[i].value` 做**整頁 golden**，涵蓋三種情境：看板、`?game=<id>`、空 DB。fixture 的 `tipoff_utc` 必須帶時區；固定時間（凍結 `datetime.now`）。整頁 golden 同時涵蓋 hero、戰績格、球隊表區塊與詳情頁組裝。搬到 `render.py` 後（純搬動 commit）必須逐字相同，fixture 的名字不含特殊字元。
   escape 另在後一個 commit 以含 `'`、`&`、`<script>` 的單元測試驗證；整頁 golden 預期不變（見第 7 節）。整頁 golden 也鎖住 caption、30 隊選單與 expander 標題。現有行為（例如終場比分遇到 `None` 時顯示 `105.0`）由 golden 鎖住，純搬動不修，要修另開 commit。
2. **渲染測試**：空資料、只有未開賽、只有已終場、缺盤口、無大小分、傷病為空。
3. **建置腳本測試**：用假的 `run` 產生完整 `site/`；檢查檔案齊全、每個站內連結都指到存在的檔案、無以 `/` 開頭的絕對路徑。
4. **HTML escape 測試**：球員名含 `<script>` 等字元時被轉義。
5. **唯讀測試**：`queries.py` 無寫入語句。
6. **過期提示**：內嵌更新時間格式正確、36 小時門檻正確。

真實資料的建置在 CI 驗證（`secrets.toml` 雖在本機，但 Claude 的讀取被 deny 擋住，程式自己讀的 `streamlit run` 另算）。本機用假資料建 `site/`，掛在 `/nba-predict/` 子路徑用 `python -m http.server` 實際點連結。

## 9. 上線順序（每個關卡都等使用者）

1. 寫特徵化測試，確認目前全綠。
2. 純搬動 commit：抽出 `render.py`、`queries.py`；Streamlit 改為 import；整頁 golden 逐字相同。接著跑 `streamlit run` 確認能載入（沒有 DB 連線就寫「未驗證」）。
2b. escape commit：統一 `esc()`、新增 escape 測試；golden 預期不變。
3. 寫 `build_site.py` 與測試。
4. 寫 `site.yml`（先不啟用 Pages）。
5. **需使用者同意**：push、啟用 Pages、手動觸發、確認網址能開、驗證 build 失敗時線上維持上一版（用 `workflow_dispatch` 的 `fail_build` boolean input，不 commit 壞程式碼；順便確認 `github-pages` environment 只允許預設分支）。預告使用者：`daily_nba` 停用期間，約 36 小時後頁面會一直顯示「資料可能已過期」。
6. 更新 `CLAUDE.md`（架構、指令、已知待釐清）。
7. 完成後交給 `reviewer` 審 diff。

## 10. 風險

| 風險 | 處置 |
| :- | :- |
| 搬動渲染函式時改壞 Streamlit | 先寫特徵化測試，輸出逐字比對。 |
| 資料來源被擋，網站內容變舊 | 過期提示 + 第二個子專案修復抓資料。 |
| 排程再被 60 天規則停用 | 屬第二個子專案處理。push 通常算 repo 活動，但 GitHub 文件未逐項列舉，實作時再確認。 |
| 新球季剛開打，`latest_season` 為 2026-27 但尚無 final 場次，球員表 fallback 寫死 2025-26，30 隊頁可能全是「尚無資料」 | 現有行為；記為已知限制，交給第二個子專案。 |
| 第一次部署與預期不同 | 第 9 節第 5 步用實測驗證，包含故意失敗。 |
