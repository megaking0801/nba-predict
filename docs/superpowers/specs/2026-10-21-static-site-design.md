# 靜態網站（一直在線）設計規格

日期：2026-10-21
狀態：待使用者審閱

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
- 每頁顯示「資料更新於 YYYY-MM-DD HH:MM（台北時間）」。
- 頁面內嵌建置時間（ISO 格式）。一小段 inline JS 在**超過 36 小時**沒更新時，於頁面頂部顯示「資料可能已過期」警示。
- 「今晚 N 場」等依建置當下計算的文字，一律搭配上述更新時間。

## 6. 部署、觸發與失敗處理

### 6.1 觸發

1. `workflow_run`：`daily_nba` 完成（`types: [completed]`），**不論成功或失敗**都觸發。官方文件：「A workflow run is triggered regardless of the conclusion of the previous workflow.」不下載上游 artifact。
2. `workflow_dispatch`：手動。
3. `push` 到 `main`，且路徑包含 `webui/**` 或 `.github/workflows/site.yml`。

注意：`daily_nba` 目前被停用，第 1 項要等第二個子專案恢復排程後才會動；在那之前依賴第 2、3 項。不另外加獨立排程（資料沒進來時，重建的內容也一樣；過期提示已處理誠實揭露）。

### 6.2 Jobs

- `build`：checkout → Python 3.11 → `pip install -r requirements.txt` → `python -m pytest tests -q` → `python -m webui.build_site`（環境變數帶入現有 DB secrets）→ `actions/upload-pages-artifact`（路徑 `site/`）。
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

- `webui/queries.py` 只含 `SELECT`，由測試強制（掃描 SQL 字串不得出現 `INSERT`、`UPDATE`、`DELETE`、`DROP`、`ALTER`、`CREATE`）。
- 來自 DB 的文字（球員名、傷病說明、球隊名）輸出前一律 HTML escape。Streamlit 版目前直接塞進 `unsafe_allow_html`，抽出後兩邊共用同一份 escape。
- `.streamlit/secrets.toml` 不讀、不印、不提交（沿用 `CLAUDE.md` 與 `settings.json` 的規則）。
- 開啟 Pages（把來源改為 GitHub Actions）是 repo 設定變更，**須使用者同意後才做**。

## 8. 測試

全部不需要 DB，使用固定的假資料。

1. **特徵化測試（在搬動程式碼之前先寫）**：記錄現有 Streamlit 版卡片、看板、三種玩法卡、傷病面板對固定資料的輸出；搬到 `render.py` 後必須逐字相同。
2. **渲染測試**：空資料、只有未開賽、只有已終場、缺盤口、無大小分、傷病為空。
3. **建置腳本測試**：用假的 `run` 產生完整 `site/`；檢查檔案齊全、每個站內連結都指到存在的檔案、無以 `/` 開頭的絕對路徑。
4. **HTML escape 測試**：球員名含 `<script>` 等字元時被轉義。
5. **唯讀測試**：`queries.py` 無寫入語句。
6. **過期提示**：內嵌更新時間格式正確、36 小時門檻正確。

真實資料的建置在 CI 驗證（本機無法讀取 DB 設定）。本機用假資料建 `site/`，掛在 `/nba-predict/` 子路徑用 `python -m http.server` 實際點連結。

## 9. 上線順序（每個關卡都等使用者）

1. 寫特徵化測試，確認目前全綠。
2. 抽出 `render.py`、`queries.py`；Streamlit 改為 import；測試維持全綠。
3. 寫 `build_site.py` 與測試。
4. 寫 `site.yml`（先不啟用 Pages）。
5. **需使用者同意**：push、啟用 Pages、手動觸發、確認網址能開、故意讓 `build` 失敗一次驗證線上維持上一版。
6. 更新 `CLAUDE.md`（架構、指令、已知待釐清）。
7. 完成後交給 `reviewer` 審 diff。

## 10. 風險

| 風險 | 處置 |
| :- | :- |
| 搬動渲染函式時改壞 Streamlit | 先寫特徵化測試，輸出逐字比對。 |
| 資料來源被擋，網站內容變舊 | 過期提示 + 第二個子專案修復抓資料。 |
| 排程再被 60 天規則停用 | 屬第二個子專案處理。push 通常算 repo 活動，但 GitHub 文件未逐項列舉，實作時再確認。 |
| 第一次部署與預期不同 | 第 9 節第 5 步用實測驗證，包含故意失敗。 |
