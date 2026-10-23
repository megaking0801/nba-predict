# P1 恢復每日資料：設計

日期：2026-10-23
狀態：主線與支線共同定案（使用者授權兩個 session 自行決定細節）。寫正式 DB、push、啟用或觸發 workflow 仍須使用者當下同意（§6）。
上層文件：`docs/superpowers/specs/2026-10-23-roadmap-design.md`（§6 P1）

## 1. 目的

讓每日資料重新流進 DB，並開始累積「事後補不回來」的資料（傷兵、名單、帶真實時間戳的盤口），讓台北 21:00 打開網站時看得到當天的預測。

使用者情境：台北時間**晚上九點**打開網站、晚上下注。

## 2. 目標、非目標、反目標

**目標**
- 10:30 UTC（台北 18:30）的排程用 ESPN 完成：比賽與 box score、結算、傷兵、名單、盤口、預測，之後靜態網站自動重建。
- 開賽前再抓盤口，作為 CLV 的收盤基準。
- 傷兵與名單保留歷史（不覆蓋）。
- 補齊 2026-27 開季至今的比賽。
- 抓取失敗時流程要亮紅燈，不能安靜地成功。
- 戰績每場只算一次。
- 排程不再因 60 天無活動被停用；DB 不再因閒置被暫停。
- 當天預測還沒產生時，網站明確標示。

**非目標**
- 回補舊賽季（P2）、盤口種類統計與資料品質（P3）、任何模型或特徵改動（P4、P5）。季前賽目前會進特徵（`jobs/features.py:371-374` 沒過濾 `season_type`），這是既有行為，留給 P4。
- 刪除 legacy 程式碼。

**反目標**
- 寫入 NBA 格式的 game_id（`00` 開頭）。DB 現有 4,158 場全是 ESPN event id。
- 修改或刪除 `market_lines`、`predictions` 的歷史列；修改舊表的唯一鍵。
- 開賽前的快照排程改寫晚上的正式預測。
- 放寬任何 gate 或出手門檻。

## 3. 已驗證的現況（2026-10-23，主線與支線分別查證）

- `jobs/run_daily.py` 的 STEPS：`ingest_games`（NBA CDN，寫 NBA 格式 id）、`ingest_stats`（stats.nba.com）、`settle`、`snapshot_injuries`、`espn_odds`、`snapshot_lines`、`predict_daily`。前兩步在 GitHub runner 上被擋。
- `settle`、`predict_daily`、`features` 不依賴 NBA 格式 id 或 stats.nba.com 才有的欄位（支線 grep 全 `jobs/` 查證）。`ingest_espn` 的 `UPSERT_GAMES` 會更新 status、比分、tipoff，足夠 settle 用。
- `jobs/ingest_espn.py`：`daily(days_back, days_ahead=2)` 不看季別視窗；`backfill(seasons)` 只認 `SEASON_WINDOWS`（`:237-241`，只到 2025-26），未知季印 `[WARN] unknown season; skip` 後跳過（`:289-293`）。
- `jobs/espn_http.py` 的 `get_json` 失敗只回 `None`；`ingest_dates` 把 `None` 當成「當天沒比賽」，ESPN 全掛時 `daily()` 仍印 `[OK] games=0`，後面步驟照跑。
- `snapshot_espn_odds()`（`ingest_espn.py:434-477`）每次對前後幾天的**所有**比賽各 append 一筆，不看開賽時間；只在 10:30 UTC 跑。22:00 UTC 那次只跑 `snapshot_lines`（Odds API，`USE_ODDS` 未設所以跳過）和 `predict_daily`。**目前沒有任何接近開賽的盤口快照。**
- `v_closing_lines` 取開賽前最後一筆（`captured_at <= tipoff_utc`）。
- `predict_daily.load_upcoming`（`:25-40`）預測今天到 +2 天、`status='scheduled'` 的比賽，所以同一場會在 D-2、D-1、D 各產生一筆預測。現在 DB 每場只有 1 筆（2,938 對 2,938），恢復每日預測後會變多筆。
- `webui/queries.py` 的 `track_record()` 直接 `count(*) FROM predictions`，多筆會被重複計算。`jobs/calibrate.py:31-41、55-65` 已經用 `DISTINCT ON (game_id) ... ORDER BY predicted_at DESC`，不受影響。
- `is_paper_mode`（`predict_daily.py:87-98`）：`PAPER_MODE` 環境變數可強制；否則以第一筆預測（2026-06-18）加 4 週判斷，所以現在**已經不是** paper 模式。
- `jobs/teams.py` 的 team id 是 NBA id（1610612747 之類）；ESPN team id 不同（LAL=13、NY=18 等，支線以 `/teams` 實測 30 隊）。
- ESPN `injuries` 每筆有 `date`（ESPN 更新那筆傷病的時間，例如 `2026-10-19T19:38Z`，支線實測）。
- ESPN roster 回應是平的 `athletes` 清單（LAL 實測 18 人），含 position、jersey、status、injuries。
- 靜態網站已有 36 小時「資料可能已過期」，以 `data_at = max(predicted_at)` 為準（`webui/render.py:857`、`:897-933`）；`static_page` 只有靜態站使用，Streamlit golden 走 `auto_nba_predict.py`。
- DB 沒有任何 2026-27 的比賽；最後一場是 2026-06-13。repo 沒有任何 Actions variables；DB secrets 最後更新 2026-06-17。

## 4. 設計

### 4.1 每日流程 `run_daily`
新的 STEPS：
1. `ingest_espn`：`ingest_espn.daily(days_back=3, days_ahead=2)`，取代 `ingest_games`、`ingest_stats`（兩者從 STEPS 移除，模組保留）。
2. `settle`
3. `snapshot_injuries`：同時寫舊表 `injury_snapshots`（相容既有讀取者）與新表 `injury_reports`（§4.3）。
4. `snapshot_rosters`：新增（§4.3）。
5. `espn_odds`：10:30 這次照舊抓前後幾天的全部比賽（正式預測要用）。
6. `snapshot_lines`（Odds API，維持 `USE_ODDS` 開關）
7. `predict_daily`

維持「每步都跑、任何一步失敗整體 exit 1」。

**抓取失敗要亮紅燈**：`ingest_dates` 區分「scoreboard 抓失敗（`None`）」與「當天沒比賽（空清單）」。抓失敗的日期比例超過一半（含全部失敗）就 raise，訊息列出失敗日期。summary 抓失敗維持現狀（單場失敗只警告）。

### 4.2 排程（`daily_nba.yml`）
| UTC | 台北 | 做什麼 |
| :- | :- | :- |
| 10:30 每天 | 18:30 | `run_daily` 全流程（正式預測） |
| 22:30 每天 | 06:30 | 收盤快照 |
| 00:30 每天 | 08:30 | 收盤快照 |
| 02:00 每天 | 10:00 | 收盤快照 |
| 12:00 週一 | 20:00 | 重訓（維持） |

- **收盤快照** = `snapshot_espn_odds(window_minutes=150)`（只抓「尚未開賽且 150 分鐘內開賽」的比賽，利用已查出的 `tipoff_utc`）＋ `snapshot_injuries` ＋ `snapshot_lines`（仍受 `USE_ODDS` 控制）。**不跑 `predict_daily`**，正式預測只認 10:30 那次。
- 涵蓋範圍（夏令，冬令全部往後一小時仍有至少一次落在窗口內）：美東 19:00／19:30 由 22:30 UTC 那次涵蓋；20:00–22:00 由 00:30 或 02:00 UTC 涵蓋。GitHub cron 可能延遲 5–30 分鐘，150 分鐘窗口留有餘裕。精確時間是主線的決定，實作時若發現缺口可微調，不需再問。
- 步驟條件是以 `github.event.schedule == '<cron 字串>'` 比對（`daily_nba.yml:172、181-185`），每個 cron 都要對應改條件。
- legacy 的 `preload` 步驟移除排程條件，只保留 `workflow_dispatch`。程式碼不刪。
- `NBA_SEASON` 預設值改為 `2026-27`（workflow 的 inputs 與 env fallback）。
- **`PAPER_MODE=1`** 設在 workflow env：P1 期間記錄的 `pick_side` 一律是 paper。理由：現行方法的讓分回測是 ROI −3.8%。不改程式、不動門檻；P5、P6 再決定何時解除。

### 4.3 新表（只新增，可重複執行）
加進 `jobs/schema.py`（`CREATE TABLE IF NOT EXISTS`），不改任何既有表：

- `injury_reports`：每次抓取都 append。欄位：`captured_at`（非空）、`reported_at`（ESPN 那筆傷病的 `date`，可為空）、`snapshot_date_et`、`team_abbr`、`player_name`、`espn_player_id`、`status`、`detail`、`source`。索引：`(snapshot_date_et)`、`(espn_player_id, captured_at)`。
  - `reported_at` 是之後做無洩漏歷史重建的關鍵（ESPN 何時知道），比 `captured_at`（我們何時抓到）更有價值。
  - 估量：每次 100–150 列 × 每天 4 次，一年約 20 萬列、30 MB 上下（估計，未實測；休賽期實測 25 隊 79 列）。
- `roster_snapshots`：每個 ET 日一份，同日重抓就 upsert。唯一鍵 `(snapshot_date_et, team_abbr, espn_player_id)`。欄位：`captured_at`、`player_name`、`position`、`jersey`、`status`、`injury_status`。估量一年約 19 萬列、20–30 MB（估計）。
- ESPN team id **不寫死**：每次先打 `.../basketball/nba/teams`，用 `normalize_espn_abbr(team.abbreviation)` 對回標準縮寫，再用 ESPN id 打 roster。不動 `jobs/teams.py` 的 `TEAMS`（它是 `ABBR_TO_TEAM_ID` 的來源）。未知縮寫報錯。

### 4.4 一次性工作
- 補齊 2026-27：在 `SEASON_WINDOWS` 加 `"2026-27": (date(2026, 10, 1), date(2027, 6, 30))`，執行 `python -m jobs.ingest_espn --seasons 2026-27`（只寫已完賽；未開賽的由每日 `daily()` 補）。主線決定加視窗而不是用 `--days-back`：結果不受執行日期影響，P2 也要用同一套視窗。會寫正式 DB。
- 驗證 secrets：手動觸發一次唯讀的 `v2_diagnose`，確認連到 Project2（DB 約 40 MB、三季資料）。

### 4.5 防止 60 天停用
- 在 `daily_nba.yml` 加一個 `keepalive` job：只在週一的排程執行，`permissions: actions: write`，用 `gh api -X PUT repos/${{ github.repository }}/actions/workflows/daily_nba.yml/enable` 重新啟用自己。不 commit、不 push、不用第三方 action。
- 依據：社群專案 gh-workflow-keepalive 的 README 寫「uses GitHub API to preemptively re-enable the workflow」、「will not create any dummy commits」，範例 job 設 `actions: write`，並放在被保護的排程 workflow 內。**GitHub 官方文件沒有說明 enable API 會重置 60 天計時**（支線實際讀過官方頁面），所以這是社群做法，**未驗證**。
- 補強：上線後約 50 天（合併到 `main` 後計算）人工跑一次 `gh workflow list` 確認仍為 active；交接文件記下這個日期。網站的 36 小時過期提示也會讓停擺被看見。
- 只有排程 workflow 會被停用；`site.yml` 靠 `workflow_run` 觸發，不受影響。
- 排程只在預設分支生效，合併到 `main` 才有作用。

### 4.6 戰績每場只算一次
- `webui/queries.py` 的 `track_record()`：已結算預測的計數與命中率，改成每場只取一筆，**優先取開賽前最後一筆**（`DISTINCT ON (game_id) ... ORDER BY game_id, (predicted_at < tipoff_utc) DESC NULLS LAST, predicted_at DESC`）。勝負與大小分兩組都改。
  - 不能用 `WHERE predicted_at < tipoff_utc` 過濾：現有 2,938 筆歷史預測是 2026-06-18 一次回填，`predicted_at` 晚於所有比賽的開賽時間，過濾會讓戰績歸零（主線寫計畫時發現，2026-10-23）。
- 因為收盤快照不再預測，「開賽前最後一筆」就是比賽當天 10:30 UTC 的正式預測。
- SQL 中保留 `(p_home_win>=0.5)` 與 `(p_over>=0.5)` 這兩段字串：Streamlit golden 的假 DB 依子字串路由，路由必須恰好命中一個 key。

### 4.7 網站：「今天的推薦還沒出來」
- `queries.next_tipoff(run)`：`SELECT min(tipoff_utc) FROM games_v2 WHERE status='scheduled' AND tipoff_utc > now()`。
- `static_page(..., next_tip=None)` 內嵌 `data-next-tip`（ISO、秒精度、帶時區，與 `data-built` 同一套驗證）。
- **判斷在瀏覽時由 JS 進行**（頁面可能建置後好幾小時才被看）：`nextTip > now` 且 `nextTip - now < 24 小時` 且 `now - dataAt > 20 小時` 才顯示「今天的推薦還沒出來，以下是 X 的預測」，X 為 `data_at` 的台北日期。`next_tip` 缺失或沒有比賽時不顯示。
- 既有的 36 小時過期提示保留。只動 `webui/`，Streamlit golden 不受影響。

## 5. 驗證與測試

`ingest_*`、`predict_daily`、schema 目前沒有測試，新程式先寫測試：
- `run_daily` 的 STEPS 不含 `ingest_games`／`ingest_stats`。
- `ingest_dates`：scoreboard 全部或過半回 `None` 時 raise；全部是空清單（沒比賽）時不 raise。
- `snapshot_espn_odds` 的 150 分鐘窗口：已開賽、超過窗口的不抓。
- `injury_reports` 是 append；`reported_at` 解析。
- 名單解析：用存檔的 ESPN 回應樣本（放 `tests/`，不打網路）；ESPN 縮寫對回標準縮寫，未知縮寫報錯。
- `track_record` 每場只算一次（FakeDb 給同一場多筆預測）。
- 網站提示：20 小時邊界、無比賽不顯示、`next_tip` 缺失不顯示；FakeDb 補 `min(tipoff_utc)` 路由。
- `python -m pytest tests -q` 全過，Streamlit golden 不變。
- schema：說明可重複執行；對正式 DB 實際跑過之前標「未驗證」。
- 上線後：看第一次 10:30 UTC 排程的 Actions 紀錄，並用 MCP 唯讀查詢新列數。

## 6. 需要使用者當下同意的操作（計畫最後獨立一章，逐項等同意）

1. 對正式 DB 執行 schema（新表）。
2. 補齊 2026-27（寫入正式 DB）。
3. push 分支、合併到 `main`（P0 先合併或一併合併）。
4. 啟用 `daily_nba`。
5. 觸發 `v2_diagnose` 驗證 secrets。

## 7. 分工與分支

- 主線（Opus）寫實作計畫；支線（Sonnet）照計畫實作；完成後交 `reviewer`（Opus）審 diff。
- CLAUDE.md 規定動 schema、ingest 前先進 plan mode：實作計畫即為該 plan，實作者開工前仍依 CLAUDE.md 進 plan mode 確認。
- P1 從 `static-site` 開分支（§4.7 要改 `webui/`），合併順序 P0 → P1。

## 8. 交給實作者

- 新表欄位型別與索引細節、名單解析的程式結構、測試樣本的存放方式。
- `ingest_dates` 失敗比例門檻的實作細節（以「過半」為準）。
