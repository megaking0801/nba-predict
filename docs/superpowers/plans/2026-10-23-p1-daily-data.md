# P1 恢復每日資料 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每日流程改走 ESPN、開始累積傷兵／名單／收盤盤口歷史、戰績每場只算一次、網站在當天預測未產生時明確標示。

**Architecture:** `run_daily` 換掉被擋的兩步，新增「收盤快照」模式（`--close`）給三個新 cron；兩張只新增的新表；`webui` 加一個查詢與一段 inline JS。所有寫正式 DB、push、啟用／觸發 workflow 的動作集中在最後一章，逐項等使用者同意。

**Tech Stack:** Python 3.11、psycopg2、pandas、pytest、GitHub Actions。不新增依賴。

**Spec:** `docs/superpowers/specs/2026-10-23-p1-daily-data-design.md`（必讀）。上層：`docs/superpowers/specs/2026-10-23-roadmap-design.md`。

## Global Constraints

- 遵守 `CLAUDE.md` 全部規則。動 schema、ingest 前依 CLAUDE.md 先進 plan mode 確認本計畫。
- 分支：從 `static-site` 開 `p1-daily-data`。合併順序 P0 → P1。
- 檔案讀寫一律 `encoding="utf-8"`（本機預設 cp950）。
- 測試不打網路、不連 DB：ESPN 回應用存檔樣本，DB 用 monkeypatch 假連線或 `tests/fakes.py` 的 `FakeDb`。
- 隊名只走 `jobs/teams.py`：`normalize_espn_abbr`、`require_abbr`、`abbr_from_team_name`。未知縮寫 raise（既有 `snapshot_injuries` 對未知隊名「警告後略過」的行為保留不改）。
- 不改 `market_lines`、`predictions`、`injury_snapshots` 的結構與唯一鍵；新表只用 `CREATE TABLE IF NOT EXISTS`。
- Streamlit golden（`tests/test_streamlit_golden.py`）不得變動；`git diff --stat tests/golden` 必須為空。
- 數值：失敗門檻「過半」；收盤快照窗口 150 分鐘；網站提示門檻 20 小時、比賽窗口 24 小時；`PAPER_MODE=1`；`NBA_SEASON` 預設 `2026-27`；2026-27 視窗 `date(2026,10,1)`–`date(2027,6,30)`。
- cron（UTC）：`30 10 * * *`（全流程）、`30 22 * * *`、`30 0 * * *`、`0 2 * * *`（收盤快照）、`0 12 * * 1`（重訓＋keepalive）。移除 `0 22 * * *`。
- 每個 task 結束跑 `python -m pytest tests -q`，貼出實際輸出。commit 訊息英文，結尾 `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`（依實際提交模型填）。
- **不 push、不寫正式 DB、不觸發或啟用 workflow**，直到第 7 章逐項取得使用者同意。

## Review Focus

1. 回填的歷史預測（`predicted_at` 2026-06-18，晚於所有歷史比賽的開賽時間）在新的 `track_record` 仍要被計入；只要求「同一場有開賽前預測時優先取開賽前最後一筆」。→ Task 5 的 SQL 與主線的唯讀驗證。
2. ESPN 部分日期抓失敗（例如 5 天裡 2 天回 `None`）不 raise；過半才 raise；全部「沒比賽」不 raise。→ Task 1。
3. 延期或已過開賽時間仍是 `scheduled` 的比賽：收盤快照不抓、`next_tipoff` 不算。→ Task 1、Task 6。
4. ESPN 傷病缺 `date` 或格式異常：`reported_at` 為 `None`，不中斷整批。→ Task 2。
5. ESPN `/teams` 回傳未知縮寫：raise 並在訊息中列出縮寫，不寫入任何名單列。→ Task 3。

---

### Task 1: `ingest_espn`：2026-27 視窗、抓取失敗要亮紅燈、收盤窗口

**Files:**
- Modify: `jobs/ingest_espn.py`（`SEASON_WINDOWS` `:237-241`、`ingest_dates` `:244-286`、`snapshot_espn_odds` `:434-477`）
- Test: `tests/test_ingest_espn.py`（新）

**Interfaces:**
- Produces: `ingest_dates(dates: list[str], finals_only: bool) -> tuple[int, int, int]`（行為新增：scoreboard 回 `None` 的日期數 × 2 > `len(dates)` 時 `raise RuntimeError`，訊息含失敗日期清單）。
- Produces: `in_close_window(tipoff_utc: datetime | None, now: datetime, window_minutes: int) -> bool`（純函式；`tipoff_utc` 為 `None` 回 `False`；`now < tipoff_utc <= now + window` 回 `True`）。
- Produces: `snapshot_espn_odds(days_back: int = 3, days_ahead: int = 2, window_minutes: int | None = None) -> None`（`None` 時行為與現在完全相同；有值時只抓 `in_close_window(...)` 為真的比賽）。

- [ ] **Step 1: 寫失敗測試** `tests/test_ingest_espn.py`（monkeypatch `ingest_espn.scoreboard`、`ingest_espn.db_connect`；`db_connect` 被呼叫就 `raise AssertionError`，確保這幾個情境在寫 DB 前就結束）：
  - `test_season_window_2026_27`：`SEASON_WINDOWS["2026-27"] == (date(2026,10,1), date(2027,6,30))`。
  - `test_ingest_dates_raises_when_majority_failed`：5 個日期中 3 個回 `None`、2 個回 `{"events": []}` → `pytest.raises(RuntimeError)`，訊息含失敗的日期字串。
  - `test_ingest_dates_tolerates_minority_failure`：5 個中 2 個 `None`、3 個 `{"events": []}` → 回 `(0, 0, 0)`。
  - `test_ingest_dates_all_empty_is_ok`：全部 `{"events": []}` → `(0, 0, 0)`。
  - `test_in_close_window`：`now=2026-10-23T22:30Z`、窗口 150：開賽 `23:00Z` → True；`01:00Z`（剛好 150 分鐘）→ True；`01:01Z` → False；`22:29Z`（已開賽）→ False；`None` → False。
  - `test_snapshot_window_filters_games`：monkeypatch `db_connect` 回假連線（第一次 `fetchall` 回三場：已開賽、60 分鐘後、300 分鐘後）、`odds` 記錄被呼叫的 game_id、`parse_close_line` 回固定 dict；`snapshot_espn_odds(window_minutes=150)` 後只對 60 分鐘那場呼叫 `odds`。
- [ ] **Step 2: 跑** `python -m pytest tests/test_ingest_espn.py -q`，Expected：FAIL（`KeyError: '2026-27'`、`ImportError: in_close_window` 等）。
- [ ] **Step 3: 實作。** `ingest_dates` 在 scoreboard 階段計數 `None`；`snapshot_espn_odds` 取得 `now_utc` 後先過濾 `games`（已有 `tipoff_utc` 欄位）。`window_minutes` 為 `None` 時不得改變現有行為。
- [ ] **Step 4: 跑** `python -m pytest tests -q`，Expected：全 PASS。
- [ ] **Step 5: Commit** `git add jobs/ingest_espn.py tests/test_ingest_espn.py && git commit -m "Fail loudly on ESPN outages, add 2026-27 window and close-snapshot window"`

---

### Task 2: 新表與 `injury_reports`（append、含 `reported_at`）

**Files:**
- Modify: `jobs/schema.py`（`DDL` 末尾加兩張表；`SCHEMA_VERSION` `"2"` → `"3"`）
- Modify: `jobs/snapshot_injuries.py`
- Create: `tests/fixtures/espn/injuries_sample.json`（從 `https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries` 存一份，裁成 2 隊、每隊 2 人；其中 1 人刪掉 `date`）
- Test: `tests/test_snapshots.py`（新）

**Interfaces:**
- Produces（`jobs/schema.py`）：
  - `public.injury_reports`：`report_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`、`captured_at TIMESTAMPTZ NOT NULL`、`reported_at TIMESTAMPTZ`、`snapshot_date_et DATE NOT NULL`、`team_abbr TEXT NOT NULL REFERENCES public.teams(team_abbr)`、`player_name TEXT NOT NULL`、`espn_player_id BIGINT`、`status TEXT`、`detail TEXT`、`source TEXT NOT NULL DEFAULT 'espn'`；索引 `(snapshot_date_et)`、`(espn_player_id, captured_at)`。無唯一鍵。
  - `public.roster_snapshots`：`snapshot_date_et DATE NOT NULL`、`captured_at TIMESTAMPTZ NOT NULL`、`team_abbr TEXT NOT NULL REFERENCES public.teams(team_abbr)`、`espn_player_id BIGINT NOT NULL`、`player_name TEXT NOT NULL`、`position TEXT`、`jersey TEXT`、`status TEXT`、`injury_status TEXT`、`PRIMARY KEY (snapshot_date_et, team_abbr, espn_player_id)`。
- Produces（`jobs/snapshot_injuries.py`）：
  - `parse_injuries(payload: dict, today: date) -> list[dict]`，每個 dict 鍵：`snapshot_date_et, team_abbr, player_name, espn_player_id, status, detail, reported_at`（`reported_at` 為 tz-aware UTC `datetime` 或 `None`；格式如 `"2026-10-19T19:38Z"`）。`fetch_injury_rows()` 改為呼叫它。
  - `INSERT_REPORT_SQL`：`INSERT INTO public.injury_reports (...)`，**不含** `ON CONFLICT`。
  - `main()`：同一個 `captured_at = now(UTC)` 寫入整批；舊表 `UPSERT_SQL` 照寫（相容），新表 append。

- [ ] **Step 1: 寫失敗測試** `tests/test_snapshots.py`：
  - `test_schema_has_new_tables_idempotent`：`DDL` 含 `CREATE TABLE IF NOT EXISTS public.injury_reports` 與 `CREATE TABLE IF NOT EXISTS public.roster_snapshots`；`SCHEMA_VERSION == "3"`；`MIGRATIONS` 不含 `DROP`。
  - `test_parse_injuries_reads_reported_at`：用樣本；有 `date` 的那筆 `reported_at == datetime(2026,10,19,19,38,tzinfo=UTC)`（依你存的樣本實際值改寫，並在測試註解寫明）；缺 `date` 的那筆 `reported_at is None`；`team_abbr` 都是標準縮寫。
  - `test_injury_reports_insert_is_append`：`"ON CONFLICT" not in snapshot_injuries.INSERT_REPORT_SQL`。
  - `test_main_writes_both_tables_with_one_timestamp`：monkeypatch `requests.get` 回樣本、`db_connect` 回記錄 `execute` 的假連線、`ensure_schema` 為 no-op；斷言舊表 upsert 與新表 insert 都有執行，且新表各列的 `captured_at` 相同。
- [ ] **Step 2: 跑** → FAIL。
- [ ] **Step 3: 實作。**
- [ ] **Step 4: 跑** `python -m pytest tests -q` → 全 PASS。另說明：新 DDL 只用 `IF NOT EXISTS`，可重複執行；**未對正式 DB 執行過**。
- [ ] **Step 5: Commit** `git commit -m "Add injury_reports and roster_snapshots tables; append injury history with ESPN report time"`（含新檔 `git add`）

---

### Task 3: `snapshot_rosters`

**Files:**
- Create: `jobs/snapshot_rosters.py`
- Create: `tests/fixtures/espn/teams_sample.json`（`/teams` 回應，保留完整 30 隊的 `id`、`abbreviation` 即可）、`tests/fixtures/espn/roster_sample.json`（`/teams/13/roster`，裁成 3 人，至少 1 人有 `injuries`）
- Test: `tests/test_snapshots.py`

**Interfaces:**
- Consumes: Task 2 的 `public.roster_snapshots`。
- Produces（`jobs/snapshot_rosters.py`）：
  - `parse_teams(payload: dict) -> dict[str, str]`：標準縮寫 → ESPN team id，走 `normalize_espn_abbr`；任何未知縮寫 `raise ValueError`（訊息含縮寫）；結果不足 30 隊也 `raise ValueError`。
  - `parse_roster(payload: dict, team_abbr: str, today: date) -> list[dict]`，鍵：`snapshot_date_et, team_abbr, espn_player_id (int), player_name, position (position.abbreviation), jersey, status (status.name), injury_status (injuries[0].status 或 None)`。
  - `UPSERT_ROSTER_SQL`：`ON CONFLICT (snapshot_date_et, team_abbr, espn_player_id) DO UPDATE`。
  - `main() -> None`：打 `/teams` → 每隊打 `/teams/{id}/roster`（用 `jobs.espn_http.get_json`）→ 同一個 `captured_at` 寫入。任何一隊 roster 抓不到（`None`）就在全部寫完後 `raise RuntimeError` 列出隊伍（其他隊仍寫入）。
- [ ] **Step 1: 寫失敗測試**：`test_parse_teams_maps_30_teams`（`len == 30`、`["LAL"] == "13"`）、`test_parse_teams_unknown_abbr_raises`（把樣本某隊改成 `"ZZZ"` → `ValueError`，訊息含 `ZZZ`）、`test_parse_roster_fields`（依樣本斷言 3 筆、`espn_player_id` 是 int、有傷病那筆 `injury_status` 非 None）、`test_roster_upsert_is_per_day`（`"ON CONFLICT (snapshot_date_et, team_abbr, espn_player_id)" in UPSERT_ROSTER_SQL`）。
- [ ] **Step 2: 跑** → FAIL。**Step 3: 實作。** **Step 4: 跑** `python -m pytest tests -q` → PASS。
- [ ] **Step 5: Commit** `git commit -m "Snapshot ESPN team rosters daily"`（含新檔）

---

### Task 4: `run_daily` 改接 ESPN、收盤模式；`daily_nba.yml`

**Files:**
- Modify: `jobs/run_daily.py`
- Modify: `.github/workflows/daily_nba.yml`
- Test: `tests/test_run_daily.py`（新）、`tests/test_daily_workflow.py`（新，文字型，不 import yaml）

**Interfaces:**
- Consumes: Task 1 `ingest_espn.daily`、`snapshot_espn_odds(window_minutes=150)`；Task 2 `snapshot_injuries.main`；Task 3 `snapshot_rosters.main`。
- Produces（`jobs/run_daily.py`）：
  - `STEPS` 名稱依序：`["ingest_espn", "settle", "snapshot_injuries", "snapshot_rosters", "espn_odds", "snapshot_lines", "predict_daily"]`；`ingest_espn` 步驟呼叫 `ingest_espn.daily(days_back=3, days_ahead=2)`。
  - `CLOSE_STEPS` 名稱依序：`["espn_odds_close", "snapshot_injuries", "snapshot_lines"]`；`espn_odds_close` 呼叫 `snapshot_espn_odds(window_minutes=CLOSE_WINDOW_MINUTES)`，`CLOSE_WINDOW_MINUTES = 150`。
  - `main(argv: list[str] | None = None) -> None`：`--close` 跑 `CLOSE_STEPS`，否則 `STEPS`；失敗處理沿用現有（每步都跑、有失敗 exit 1）。
- Produces（`daily_nba.yml`）：
  - schedule：`30 10 * * *`、`30 22 * * *`、`30 0 * * *`、`0 2 * * *`、`0 12 * * 1`。
  - 「v2 daily pipeline」條件不變（`'30 10 * * *'` 或 dispatch `v2_daily`/`all`）。
  - 原「v2 near-tipoff lines + re-predict」改名「v2 close snapshot」，`run: python -u -m jobs.run_daily --close`，條件為三個收盤 cron 任一，或 dispatch `v2_lines`。不再跑 `predict_daily`。
  - 「Legacy daily preload to DB」條件移除 `github.event.schedule == '30 10 * * *' ||`，只留 dispatch。
  - `NBA_SEASON` 的 input default 與 env fallback 改 `2026-27`；`season` input 說明同步。
  - job env 加 `PAPER_MODE: "1"`。
  - 新 job `keepalive`：`if: github.event.schedule == '0 12 * * 1'`；`permissions: actions: write`；單一步驟 `gh api -X PUT "repos/${{ github.repository }}/actions/workflows/daily_nba.yml/enable"`，env `GH_TOKEN: ${{ github.token }}`。不 checkout、不拿 DB secrets。

- [ ] **Step 1: 寫失敗測試**
  - `tests/test_run_daily.py`：`test_steps_use_espn`（`[n for n, _ in STEPS]` 等於上列）、`test_close_steps_never_predict`（`CLOSE_STEPS` 名稱等於上列且不含 `predict_daily`）、`test_close_window_is_150`、`test_main_close_runs_only_close_steps`（monkeypatch 兩個清單成記錄呼叫的假函式，`main(["--close"])` 只呼叫 CLOSE 的）。
  - `tests/test_daily_workflow.py`（讀檔 `encoding="utf-8"`）：`test_crons`（五個 cron 字串都在、`'0 22 * * *'` 不在）、`test_legacy_preload_not_scheduled`（`Legacy daily preload` 那個 step 的 `if:` 行不含 `github.event.schedule`）、`test_close_step_runs_close_mode`（含 `jobs.run_daily --close`，且該 step 區塊內不含 `predict_daily`）、`test_paper_mode_on`（含 `PAPER_MODE: "1"`）、`test_season_default`（含 `2026-27`，`default: "2025-26"` 不在）、`test_keepalive_job`（含 `actions: write` 與 `/actions/workflows/daily_nba.yml/enable`，且 keepalive job 區塊內不含 `secrets.`）。step／job 區塊以下一個同縮排的 `- name:` 或 job 鍵為邊界。
- [ ] **Step 2: 跑** → FAIL。**Step 3: 實作。** **Step 4: 跑** `python -m pytest tests -q` → PASS。
- [ ] **Step 5: Commit** `git commit -am "Run daily pipeline on ESPN, add close snapshots, pause legacy schedule, keep workflow alive"`（含新測試檔 `git add`）

---

### Task 5: 戰績每場只算一次

**Files:**
- Modify: `webui/queries.py`（`track_record` 的兩段 `rec`、`reco` 查詢）
- Test: `tests/test_webui_queries.py`

**Interfaces:**
- Produces: `track_record(run) -> dict`，鍵不變。兩段計數改為「每場一筆」：

```sql
WITH last AS (
  SELECT DISTINCT ON (p.game_id) p.p_home_win, p.win_result
  FROM public.predictions p JOIN public.games_v2 g ON g.game_id = p.game_id
  WHERE p.p_home_win IS NOT NULL AND p.win_result IS NOT NULL
  ORDER BY p.game_id, (p.predicted_at < g.tipoff_utc) DESC NULLS LAST, p.predicted_at DESC
)
SELECT count(*) FILTER (WHERE (p_home_win>=0.5)=(win_result=1)) AS hit, count(*) AS n FROM last
```
  大小分同理（`p_over`、`over_result IN (0,1)`、`(p_over>=0.5)`）。
  **與設計 §4.6 的差異（主線決定）**：不用 `WHERE predicted_at < tipoff_utc` 過濾，改成排序偏好。理由：現有 2,938 筆歷史預測是 2026-06-18 一次回填，`predicted_at` 晚於所有比賽的開賽時間，用 WHERE 過濾會讓戰績歸零。

- [ ] **Step 1: 寫失敗測試**：`test_track_record_dedups_per_game`：攔截 `run` 收到的 SQL，斷言兩段都含 `DISTINCT ON (p.game_id)`、`(p.predicted_at < g.tipoff_utc) DESC NULLS LAST`，且分別含 `(p_home_win>=0.5)`、`(p_over>=0.5)`；回傳鍵集合不變。
- [ ] **Step 2: 跑** → FAIL。**Step 3: 實作。**
- [ ] **Step 4: 跑** `python -m pytest tests -q` → PASS，且 `git diff --stat tests/golden` 為空（FakeDb 依子字串路由，每段 SQL 仍恰好命中一個 key）。
- [ ] **Step 5: 交主線驗證**：主線用唯讀 MCP 對正式 DB 執行新舊兩組 SQL，確認目前 `n`、`hit` 與舊查詢相同（每場只有 1 筆時兩者應一致）。結果貼回本計畫的執行紀錄。
- [ ] **Step 6: Commit** `git commit -am "Count each game once in the live track record"`

---

### Task 6: 網站「今天的推薦還沒出來」

**Files:**
- Modify: `webui/queries.py`、`webui/render.py`（`static_page` `:897-933`）、`webui/build_site.py`（`_page` 與所有 `static_page` 呼叫處）、`tests/fakes.py`
- Test: `tests/test_webui_render.py`、`tests/test_webui_build.py`、`tests/test_webui_queries.py`

**Interfaces:**
- Produces（`queries.py`）：`next_tipoff(run) -> datetime | None`，SQL：`SELECT min(tipoff_utc) AS tipoff_utc FROM public.games_v2 WHERE status='scheduled' AND tipoff_utc > now()`；回傳 tz-aware UTC（同 `data_freshness` 的處理方式）。
- Produces（`render.py`）：
  - `NOT_YET_HOURS = 20`、`NEXT_GAME_WINDOW_HOURS = 24`。
  - `should_show_not_yet(now: datetime, data_at: datetime | None, next_tip: datetime | None) -> bool`：參考實作，條件 `next_tip is not None and data_at is not None and now < next_tip and next_tip - now < 24h and now - data_at > 20h`。JS 必須與它等價。
  - `static_page(title, body_html, built_at, data_at=None, next_tip=None) -> str`：`next_tip` 非 None 時必須 tz-aware（否則 `ValueError`），並在 `#built` 元素加 `data-next-tip="<ISO 秒精度>"`；頁首加 `<div id="notyet" class="nb-stale" hidden>今天的推薦還沒出來，以下是 {data_at 的台北日期 %m/%d} 的預測。</div>`（`data_at` 為 None 時不輸出此 div）；inline JS 依 `data-built`（已是 data_at）與 `data-next-tip` 套用同一條件，常數寫成 `20*3600*1000`、`24*3600*1000`。
- Produces（`build_site.py`）：每頁都傳 `next_tip = queries.next_tipoff(run)`。
- Produces（`tests/fakes.py`）：`scenario_board` 加路由 `"min(tipoff_utc)"` → 一個未來時間；`scenario_offseason`、`scenario_empty` → `None`。路由必須恰好命中一個 key。

- [ ] **Step 1: 寫失敗測試**
  - `test_should_show_not_yet_boundaries`（`render`）：`now=2026-10-23T13:00Z`；`data_at` 20 小時又 1 分鐘前、`next_tip` 10 小時後 → True；`data_at` 剛好 20 小時前 → False；`next_tip` 25 小時後 → False；`next_tip` 已過 → False；`next_tip=None` → False；`data_at=None` → False。
  - `test_static_page_next_tip_marker`：有 `next_tip` 時含 `data-next-tip="2026-10-24T23:00:00+00:00"`、含 `id="notyet"`、含 `20*3600*1000` 與 `24*3600*1000`；`next_tip=None` 時不含 `data-next-tip`；naive `next_tip` → `ValueError`。
  - `test_next_tipoff_query`（`queries`）：用 `scenario_board().run` 回 tz-aware datetime；`scenario_empty().run` 回 None。
  - `test_not_yet_marker_in_every_page`（`build`）：`scenario_board` 建站後每個 `.html`（`404.html` 除外依現有慣例判斷）都含 `data-next-tip`。
- [ ] **Step 2: 跑** → FAIL。**Step 3: 實作。**
- [ ] **Step 4: 跑** `python -m pytest tests -q` → PASS；`git diff --stat tests/golden` 為空。
- [ ] **Step 5: 本機點看**：用 `scenario_board().run` 建站到 scratchpad，`python -m http.server` 開首頁，手動把系統時間條件模擬成「超過 20 小時」（在瀏覽器 console 覆寫 `Date.now` 後重跑該段 JS，或暫時改 fixture 的 `data_at`），確認提示出現與消失；無法開瀏覽器就寫「未驗證」。
- [ ] **Step 6: Commit** `git commit -am "Show a notice when today's predictions have not been produced yet"`

---

### Task 7: 文件、全面驗證、審查

**Files:**
- Modify: `CLAUDE.md`（架構：`run_daily` 走 ESPN、`--close` 收盤快照、兩張新表、`PAPER_MODE=1`、keepalive；排程表改成五個 cron；已知待釐清：移除「`ingest_stats` 走哪條」那條，新增「keepalive 重置 60 天計時未經官方證實，合併後約 50 天人工檢查」）
- Modify: `docs/handoff/2026-10-23-roadmap.md`（P1 狀態）

- [ ] **Step 1: 更新文件並 commit** `git commit -am "Document the P1 daily pipeline"`。
- [ ] **Step 2: 全面驗證並貼出輸出**：`python -m pytest tests -q`；`git diff --stat static-site..HEAD -- tests/golden` 為空；`git diff static-site..HEAD --stat`。
- [ ] **Step 3: 交 `reviewer`（Opus）審 `git diff static-site..HEAD`**。prompt 重述：不得寫 NBA 格式 game_id、舊表結構不變、新表只用 IF NOT EXISTS、收盤快照不預測、`track_record` 不得讓回填歷史歸零、DB secrets 不進 keepalive job、golden 不變、不可 push。修完 Critical／High 再回報。

---

## 第 7 章：需要使用者當下同意的操作（逐項分別問，順序如下）

每一項都要使用者明確說「好」才做，做完貼出結果。主線負責詢問與用唯讀 MCP 驗證。

1. **push 並合併**：push `p1-daily-data`；P0（`static-site`）與 P1 依序合併到 `main`（方式：PR 或本機 merge 後 push，由使用者選）。
2. **對正式 DB 建表**：`python -m jobs.schema`（需 DB 連線；在 Actions 用 `workflow_dispatch` 的 `v2_schema`，或使用者本機執行）。驗證：MCP 查 `injury_reports`、`roster_snapshots` 存在、`schema_meta.schema_version = '3'`。
3. **驗證 secrets**：觸發 `v2_diagnose`，確認輸出是 Project2 的資料量（約 4,158 場、三季）。
4. **補齊 2026-27**：`python -m jobs.ingest_espn --seasons 2026-27`（需新增 dispatch 選項或使用者本機執行；由主線屆時提出最小做法）。驗證：MCP 查 2026-27 場數與最新日期。
5. **啟用排程**：`gh workflow enable daily_nba`。驗證：下一次 10:30 UTC 執行的 Actions 紀錄、MCP 查新列數（games、injury_reports、roster_snapshots、market_lines）。
6. **記錄檢查日**：在交接文件寫下「合併日 + 50 天」人工跑 `gh workflow list` 的日期。
