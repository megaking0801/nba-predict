# P2 補資料：設計

日期：2026-10-24
狀態：主線與支線共同定案（使用者授權兩個 session 決定細節；寫正式 DB、push 到 main、觸發 workflow 仍須使用者同意）
上層：`docs/superpowers/specs/2026-10-23-roadmap-design.md` §6 P2；P1 已上線（`docs/handoff/2026-10-23-roadmap.md`）。

## 1. 目的

把 2015-16 到 2022-23 共 8 季補進 DB，並為**全部 12 季**（含現有 3 季與進行中的 2026-27）補上 P4 特徵需要、而現在沒存的資料：先發、沒上場與原因、裁判、中立場地、出手距離分布，以及有標示時間點的盤口。

## 2. 目標、非目標、反目標

**目標**
- 8 季的比賽、球隊 box score、球員 box score、盤口進 DB。
- 新增資料（全部季別）：球員出賽狀態（先發、DNP、原因、驅逐）、裁判、`neutral_site`、各隊每場的出手距離分布。
- 盤口標示讓分與總分各自取的是哪個時間點（close／current／open）。
- 補回現有缺漏的 box score（約 2023-24 18 場、2024-25 3 場）。
- 每日流程（`ingest_espn.daily`）也寫入上述新資料。

**非目標**
- 資料品質檢查與修正（P3）、特徵（P4）、模型（P5）、on/off 與場上陣容（P7）。

**反目標**
- 刪除或覆蓋 P1 上線後累積的即時盤口快照（`market_lines` 中 `source='espn'` 且 `captured_at` 不等於開賽時間的列）。
- 在 `player_game_stats` 新增 DNP 列：`webui/queries.py:117-125` 的球員場均（`count(*) AS 場次`、各項平均、`HAVING avg(min_played) >= 10`）與 `jobs/diagnose.py:98` 的列數會被 0 分鐘列稀釋或改變。`jobs/features.py:320` 目前有 `min_played > 0` 的防線，但不應依賴它。
- 寫入 NBA 格式 game_id；未知縮寫補零。
- 存整包原始 JSON（每場 summary 約 330–520 KB，12 季約 5 GB）。
- 改變 `parse_summary` 既有兩組輸出（球隊列、球員列）的任何值。

## 3. 已驗證的現況（2026-10-24，主線與支線分別查證）

- `ingest_espn.parse_summary`（`jobs/ingest_espn.py:111-218`）回傳 `(team_rows, player_rows)`，略過 `didNotPlay` 與 `stats` 為空的球員；不讀 `starter`、`reason`、`ejected`、裁判、中立場地、`plays`。
- `main()` 對 `--seasons` 只呼叫 `ensure_schema` → `backfill(seasons)`；`backfill()` 只呼叫 `ingest_dates`，**不會抓盤口**。
- `backfill_odds()` 先 `DELETE FROM market_lines WHERE source='espn'` 再重灌。P1 的即時快照同為 `source='espn', book='espn'`，只能用 `captured_at = games_v2.tipoff_utc` 區分回填列。
- `parse_close_line()` 的讓分（`side_block`）與總分（`for blk in (close, current, open)`）**各自** fallback，兩者可能取自不同時間點。
- `v_latest_lines`、`v_closing_lines` 是 `SELECT ml.*`；先前加 `total_line` 等欄位時就是靠 `CREATE OR REPLACE VIEW` 把新欄位放在 view 尾端。新增 `market_lines` 欄位沿用同方式（未在真實 Postgres 對這次的新欄位驗證）。
- ESPN 路徑（實測）：裁判 `summary.gameInfo.officials[].displayName`；中立場地 `summary.header.competitions[0].neutralSite`；DNP／先發在 `boxscore.players[].statistics[0].athletes[]` 的 `starter`、`didNotPlay`、`reason`、`ejected`（DNP 球員 `stats` 為空清單；一場實測 29 人、10 先發、8 DNP）；2015 年 summary 形狀相同（有 players、plays、3 位裁判）。
- core API odds 抽樣（2015-12-01、2017-01-15、2019-03-01、2020-12-30、2022-01-10 各 4 場）：全部有盤口、9–15 家供應商，但只有 `current`。
- 出手座標：整數英尺，原點為籃框中心 (25, 0)。主線 2 場 RMSE 0.39–0.47 呎、支線 4 場 0.71–0.83 呎（文字距離取整所致）；(25, 5.25) 誤差約 3 呎，故 y 原點是籃框不是底線。有座標的出手約占全部 shootingPlay 的 78–86%（分母含罰球）。play 的 `team.id` 是 ESPN 隊伍 id，可用同一份 summary 的 `boxscore.teams[].team.id` 對回縮寫。罰球 `type.text` 形如 "Free Throw - 1 of 2"。
- 角落三分：NBA 角落三分線 22 呎、距邊線 3 呎，與 23.75 呎弧線交於 y ≈ 8.95，所以「3 分且 y ≤ 9」為角落。
- scoreboard 在沒比賽的日子（含 2020-03-15 停賽、全明星週）回 HTTP 200 與空 `events`，不會誤觸 P1 的「過半失敗就 raise」。
- 現有 `player_game_stats` 約 22 MB／9.06 萬列（約 240 B／列，含索引）。
- scoreboard 的 `events[].competitions[0].neutralSite` 也有中立場地欄位（未開賽的比賽也有）；**但 2020-08-15 泡泡賽實測為 `False`**，所以 `neutral_site` 抓不到泡泡賽，P4 要用日期區間標記泡泡（主線實測 2026-10-24）。

## 4. 設計

### 4.1 季別視窗
`SEASON_WINDOWS` 新增（ET 日期）：
| 季 | 起 | 迄 |
| :- | :- | :- |
| 2015-16 ～ 2018-19、2021-22、2022-23 | 該年 10-01 | 翌年 06-30 |
| 2019-20 | 2019-10-01 | 2020-10-15（泡泡季後賽，冠軍賽 2020-10-11 結束） |
| 2020-21 | 2020-12-01 | 2021-07-31（例行賽 2020-12-22 開打，冠軍賽 2021-07-20 結束） |

賽程日期依記憶，跑完後用 MCP 查兩季最早／最晚場次確認。

### 4.2 新表與新欄位（只新增，可重複執行）
- `games_v2` 加 `neutral_site BOOLEAN`。`v_app_board` 不動。
- `player_game_status`：`game_id`、`player_id`、`team_abbr`、`player_name`、`starter BOOLEAN`、`did_not_play BOOLEAN`、`dnp_reason TEXT`、`ejected BOOLEAN`；主鍵 `(game_id, player_id)`；**box score 列出的每位球員一列**（含有上場的），upsert。
- `game_officials`：`game_id`、`official_order SMALLINT`、`official_name TEXT`；主鍵 `(game_id, official_order)`，upsert。
- `team_shot_buckets`（細桶，P4 自行重新分組；改分區定義不必重抓）：`game_id`、`team_abbr`、`pts_value SMALLINT`（2 或 3）、`dist_bucket TEXT`、`corner BOOLEAN`、`fga INT`、`fgm INT`；主鍵 `(game_id, team_abbr, pts_value, dist_bucket, corner)`，upsert。只寫有出手的組合。
- `market_lines` 加 `price_point TEXT`（讓分取自 `'close' | 'current' | 'open'`）與 `total_price_point TEXT`（總分取自哪個）。舊列為 NULL。

### 4.3 出手距離桶
- 只計投籃：`shootingPlay` 為真，且 `type.text` 不含 "Free Throw"。
- 隊伍：play 的 `team.id` 以該場 `boxscore.teams[].team.id` 對回縮寫；沒有 `team` 或對不回的 play **不計**，數量印在 log。
- `pts_value`：`pointsAttempted`（2 或 3；其他值不計並記 log）。
- 距離 = 座標到 (25, 0) 的歐氏距離（英尺）。`dist_bucket`：`'0-4'`、`'4-8'`、`'8-14'`、`'14-24'`、`'24+'`（區間左閉右開），座標缺失或 |x| > 1000 為 `'unknown'`。
- `corner`：`pts_value == 3` 且有座標且 y ≤ 9；其他為 false。
- 座標是整數，邊界在真實資料是量化的；P4 算比例要以「已知距離的出手」當分母，`unknown` 約占 10–20%。
- 各桶 `fga` 加總應等於 `team_game_stats.fga`，差異留給 P3 統計，不在 P2 擋。

### 4.4 盤口
- `parse_close_line` 回傳值加 `price_point`、`total_price_point`；`snapshot_espn_odds` 與新函式都寫入。
- 新函式 `backfill_odds_missing(seasons: list[str]) -> None`：只對「該季已完賽、且還沒有任何 `source='espn'` 盤口」的比賽抓 core API odds，`captured_at = tipoff_utc`（讓 `v_closing_lines` 收得到）。**不刪任何列**。
  - P1 上線後的比賽已有即時快照，會被略過，它們的收盤就是最後一筆開賽前快照，不是 core API 的 close。這是刻意接受的（收盤快照本來就為此設計）。
- 既有 `backfill_odds()`：沒帶 `--force-rebuild` 時 raise 並提示改用 `backfill_odds_missing`；帶了才執行，且 DELETE 只刪回填列（`captured_at = games_v2.tipoff_utc` 的 ESPN 列），不刪整個 source。`CLAUDE.md` 的「`ingest_espn --odds` 既有重建流程」同步改寫。

### 4.5 抓取與寫入
- `parse_summary(meta, bs) -> SummaryRows`（NamedTuple：`team_rows`、`player_rows`、`status_rows`、`official_rows`、`shot_rows`）。前兩欄的值與現在**逐項相同**（先寫特徵化測試鎖住）。
- `neutral_site` 由 `parse_scoreboard_events` 從 scoreboard 的 `competitions[0].neutralSite` 取得（未開賽也有），`games_v2` 的 upsert 帶入；不從 summary 取。
- `ingest_dates` 對已完賽比賽寫入其餘新資料（同一個連線、同一個 commit）。每日流程與回補走同一條路。
- `ingest_espn --seasons ...`：每一季先 `backfill([s])`，再 `backfill_odds_missing([s])`。
- 現有 3 季與 2026-27 用同一個指令重跑（全部 upsert），補新表、新欄位與缺漏 box score。
- 不存原始 JSON 或逐回合檔案；P7 需要時重抓（12 季約 1.5 萬次請求）。推翻總體規劃「P2 存 Parquet」的理由：存放位置需要使用者新開帳號或新增 secret，重抓只花時間。風險：ESPN 日後下架舊場次的 plays（機率低）。

### 4.6 執行（第 7 章，需使用者同意）
- `workflow_dispatch` 的 `v2_backfill`，**一次一季**，由新到舊：2022-23 → … → 2015-16，再重跑 2023-24 → 2026-27。每季跑完用 MCP 唯讀查場數、新表列數、`price_point` 分布，再進下一季。
- 每季約 1,300–1,400 場，各 1 次 summary 與 1 次 odds，`MAX_WORKERS=8`；預估每季數分鐘到十幾分鐘（未實測）；job `timeout-minutes: 90`。中途 raise 不會丟失已 upsert 的資料，重跑安全。
- 儲存估計：8 季 box score 約 +60 MB；`player_game_status`（12 季約 44 萬列）約 50–70 MB；其他新表合計 < 20 MB；全部完成約 170–200 MB（上限 500 MB；估計，跑完實測）。

## 5. 驗證與測試

用存檔的 ESPN 樣本（`tests/fixtures/espn/`），不打網路、不連 DB；**先寫 `parse_summary` 現有輸出的特徵化測試，再改程式**：
- 特徵化：用存檔 summary，`team_rows`、`player_rows` 與改版前逐項相同。
- status：每位列出的球員一列；先發、DNP 與原因、驅逐正確；`player_rows` 列數不變。
- officials：3 位、順序；`neutral_site` 路徑。
- shot buckets：各桶加總等於有隊伍歸屬的投籃數；罰球不計；`unknown` 處理；邊界 3.99→`0-4`、4.0→`4-8`、14.0→`14-24`、24.0→`24+`；3 分 y=9→corner、y=10→非 corner。
- `parse_close_line`：只有 `current` 時兩個 point 皆為 `'current'`；讓分 close、總分 current 時分別標示。
- `backfill_odds_missing` 不含 `DELETE`；`backfill_odds()` 無 `--force-rebuild` 時 raise；有時 DELETE 條件含 `captured_at = g.tipoff_utc`。
- `main(["--seasons", "2022-23"])` 依序呼叫 `backfill` 與 `backfill_odds_missing`（monkeypatch）。
- `SEASON_WINDOWS` 含 8 季且 2019-20、2020-21 為上表日期。
- `python -m pytest tests -q` 全過，golden 不變。

## 6. 交給實作者
- 新表索引、樣本裁剪方式、log 格式。

## 7. 需要使用者同意（獨立一章）
1. push 並快轉 `main`。
2. 依 §4.6 逐季 dispatch `v2_backfill`（寫正式 DB；第一次執行會 `ensure_schema` 建新表與新欄位）。
