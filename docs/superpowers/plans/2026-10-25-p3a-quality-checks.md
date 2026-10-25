# P3a 資料品質檢查模組 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增只讀的 `jobs/quality/`：一組資料品質檢查、已知問題基準線、每場品質旗標函式 `game_quality()` 與 CLI `python -m jobs.quality`。

**Architecture:** 每項檢查是一個 `check_xxx(run, scope) -> list[Finding]`，SQL 只含 SELECT 且用 `/* qc:<ID> */` 註解標記，讓 `FakeDb` 以子字串路由；判斷邏輯（門檻、分類、基準線狀態機）寫成純 Python 以便單元測試。CLI 把 Findings 與 `baseline.json` 比對，輸出表格與結束碼。

**Tech Stack:** Python 3.11、pandas、psycopg2（沿用 `webui.build_site.connect_readonly`）、pytest。不新增依賴。

**Spec:** `docs/superpowers/specs/2026-10-25-p3-data-quality-design.md`（必讀，下稱「設計」；章節號以它為準）。原始數字：`docs/handoff/p3-sql-results-2026-10-25.md`。

## Global Constraints

- 遵守 `CLAUDE.md`。分支：`p3-data-quality`（已含設計）。實作者依 CLAUDE.md 先進 plan mode 請使用者在該 session 核准。
- **全模組只讀**：所有 SQL 只含 SELECT；不改任何資料、不改 `jobs/schema.py`、不改 workflow、不建檢視表（P3a.2／P3b 另案）。
- 檔案讀寫 `encoding="utf-8"`；測試不連 DB、不打網路（A1 的 ESPN 呼叫以 monkeypatch 取代）。
- 連線沿用 `webui/build_site.py` 的 `connect_readonly()`／`make_run(conn)`（`Run = Callable[[str, tuple], pd.DataFrame]`，見 `webui/queries.py`）。
- 數值以設計為準：D2 退步門檻 3 個百分點；比例型檢查可比對場次 < 200 只回報；E3 讓分相關 ∈ [0.30, 0.65]、MAE ∈ [8.0, 12.0]、`avg(margin+home_spread)` ∈ [−1.0, +1.0]；E4 總分線 ∈ [170, 280]；E5 相關 ≥ 0.25、平均誤差絕對值 ≤ 2；`--fail-on` 預設 `warn`。
- 基準線只能由「對正式 DB 實際執行檢查」產生（Task 7），不得手抄設計 §3 的數字；放寬基準線必須在 commit 訊息說明原因。
- Streamlit golden 不得變動；每個 task 結束跑 `python -m pytest tests -q` 並貼輸出。commit 訊息英文，結尾 `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`（依實際模型填）。
- **不 push、不寫正式 DB、不觸發 workflow**。

## Review Focus

1. 完賽場次為 0 的範圍（例如 2026-27 尚無某賽別）：檢查回 `n_total=0`、不除以零、不誤報。→ Task 2–5。
2. 比賽的某一隊在 `team_game_stats` 缺列：B 類與 `game_quality` 視為該場 `team_box_ok = False`，不 raise。→ Task 3、5。
3. `v_closing_lines` 沒有該場：`has_spread/has_total = False`、provenance `none`，不影響其他欄位。→ Task 5。
4. 基準線檔缺少某個範圍、或基準線之後出現全新賽別：狀態 `new`，不是 `ok`。→ Task 1。
5. 區間執行（`--since`）時沒有任何新場次：輸出「無新資料」，結束碼 0。→ Task 6。

---

### Task 1: `Finding` 與基準線狀態機（純 Python）

**Files:**
- Create: `jobs/quality/__init__.py`（先只匯出 `Finding`、`classify`、`load_baseline`、`exit_code`）、`jobs/quality/core.py`、`tests/test_quality_core.py`

**Interfaces:**
- Produces（`jobs/quality/core.py`）：
  - `@dataclass Finding`：`check_id: str`、`severity: str`（`"error"|"warn"|"info"`）、`scope: str`（形如 `2025-26/regular`）、`n_bad: int`、`n_total: int`、`detail: dict`、`baseline: int | None = None`、`status: str = "ok"`。
  - `classify(f: Finding, baseline: dict, *, since_mode: bool = False) -> Finding`：回傳設好 `baseline`、`status` 的新物件。規則：`since_mode` 時基準線視為 0（任何不良即 `new`）；否則 `n_bad == 0` → `ok`（若有基準 > 0 則 `detail["improved"]=True`）；`baseline is None` 且 `n_bad > 0` → `new`；`n_bad <= baseline` → `known`；`n_bad > baseline` → `worse`；`severity == "info"` 一律 `status="info"` 不判成敗。
  - `baseline_key(check_id: str, scope: str) -> str`：`f"{check_id}|{scope}"`。
  - `load_baseline(path: Path | None) -> dict`：檔案格式 `{"as_of_date": "YYYY-MM-DD", "entries": {"<key>": {"n_bad": int, "why": str}}}`；檔案不存在回 `{"as_of_date": None, "entries": {}}`。
  - `exit_code(findings: list[Finding], fail_on: str) -> int`：只看 `status in {"new","worse"}`；`fail_on` 為 `"error"`（只算 error）、`"warn"`（error＋warn）、`"none"`（恆 0）。
- [ ] **Step 1: 寫失敗測試** `tests/test_quality_core.py`：`test_classify_ok_known_worse_new`（n_bad 0／≤基準／>基準／基準缺）、`test_info_never_fails`、`test_since_mode_ignores_baseline`（基準 5、n_bad 1 → `new`）、`test_improved_flag`、`test_exit_code_fail_on`（warn 級 `new` 在 `error` 模式為 0、在 `warn` 模式為 1；`known` 的 error 為 0）、`test_load_baseline_missing_file`。
- [ ] **Step 2: 跑** `python -m pytest tests/test_quality_core.py -q` → FAIL。**Step 3: 實作。** **Step 4: 跑** `python -m pytest tests -q` → PASS。
- [ ] **Step 5: Commit** `git add jobs/quality tests/test_quality_core.py && git commit -m "Add quality Finding type and baseline state machine"`

---

### Task 2: A 類（場次）與 C 類（先發、裁判）檢查

**Files:**
- Create: `jobs/quality/checks_games.py`、`tests/test_quality_games.py`
- Modify: `tests/fakes.py`（不改既有；若需要新增輔助函式放在新檔 `tests/quality_fakes.py`）

**Interfaces:**
- Consumes: Task 1 的 `Finding`。
- Produces：`Scope = NamedTuple(seasons: list[str] | None, since: date | None)`（放 `core.py`）；`Run` 沿用 `webui.queries.Run`。
- Produces（`checks_games.py`，簽名 `def check_xxx(run: Run, scope: Scope) -> list[Finding]`）：`check_a2_team_game_counts`、`check_a3_duplicate_games`、`check_a4_team_twice_same_day`、`check_a5_stuck_not_final`、`check_a6_final_without_score`、`check_a7_suspicious_tipoff`、`check_c1_status_rows_present`、`check_c2_starters_are_ten`、`check_c3_officials_at_least_three`。判斷與嚴重度見設計 §4.1、§4.3；A2 的「83 場隊數 ≠ 2 → error」只對 2023-24 之後各季；2019-20 不檢查；2020-21 預期 72。每個 SQL 開頭含 `/* qc:<ID> */`，回傳每季／賽別一列的計數，Finding 在 Python 端組。
- [ ] **Step 1: 寫失敗測試**（FakeDb 路由 key 為 `qc:A2` 等；每項至少 ok 與有問題兩種）：A2 預期 82／72／2019-20 略過／83 場隊數為 2 為 ok、為 3 為 error；A3、A5、A6 計數 > 0 為 error；A4 計數回報；A7 以三種條件（小時 < 10、`tipoff_utc` 為空、ET 日期不符）各一例；C1 缺列為 error；C2、C3 的計數；每個檢查在 `n_total=0` 時回 `n_bad=0` 不 raise。另有 `test_all_sql_is_select_only`：掃描 `checks_games.py` 的字串常值，不得出現 `\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT)\b`（沿用 `tests/test_webui_queries.py` 的做法）。
- [ ] **Step 2: 跑** → FAIL。**Step 3: 實作。** **Step 4: 跑** `python -m pytest tests -q` → PASS。
- [ ] **Step 5: Commit** `git commit -am "Add game-level and status/official quality checks"`（含新檔 `git add`）

---

### Task 3: B 類（box score）檢查

**Files:**
- Create: `jobs/quality/checks_box.py`、`tests/test_quality_box.py`

**Interfaces:**
- Produces：`check_b1_team_box_complete`、`check_b2_player_box_not_placeholder`、`check_b3_player_points_match_team`、`check_b4_team_minutes_240`、`check_b5_impossible_lines`、`check_b6_team_points_equal_score`（簽名同 Task 2；規則與嚴重度見設計 §4.2）。
  - B1：兩隊 `fga, fgm, fg3a, fta, tov, oreb, dreb` 皆非 NULL；缺一隊的列視為不合格。
  - B2：每隊球員列中 `min_played` 非 NULL 的列數 > 0。
  - B3：只對 B2 通過的隊場；單場差 > 3 的數量放 `detail["off_gt_3"]`。
  - B4：球員分鐘合計與 `240 + 25*k`（k 為最接近的整數，≥ 0）相差 ≤ 3。
  - B5：`fgm > fga`、`ftm > fta`、`fg3m > fg3a`、`pts < 0`、`min_played > 60`。
  - B6：`team_game_stats.pts` ≠ 比賽對應比分。
- [ ] **Step 1: 寫失敗測試**：每項 ok／有問題；B3 對佔位隊場（B2 失敗）不計入 `n_total`；B4 邊界（240、265、243 ok；244 不合格）；缺一隊 `team_game_stats` 列時 B1 計為不合格不 raise；SQL 只讀掃描。
- [ ] **Step 2: 跑** → FAIL。**Step 3: 實作。** **Step 4: 跑** `python -m pytest tests -q` → PASS。
- [ ] **Step 5: Commit** `git commit -am "Add box-score quality checks"`（含新檔）

---

### Task 4: D 類（出手桶）與 E 類（盤口）檢查

**Files:**
- Create: `jobs/quality/checks_shots.py`、`jobs/quality/checks_lines.py`、`tests/test_quality_shots.py`、`tests/test_quality_lines.py`

**Interfaces:**
- Produces（`checks_shots.py`）：
  - `shots_class(box_fga: int | None, bucket_fga: int | None) -> str`：`no_box`（box 為 None）、`none`（桶為 None 或 0 列）、`exact`（差 0）、`minor`（|差| ≤ 2）、`major`。
  - `check_d1_shots_present`、`check_d2_shots_class_share`、`check_d3_shots_3pa_match`、`check_d4_shots_unknown_share`。D2：每季每賽別，分母只計非 `no_box`；可比對 < 200 只回報 info；比基準線退步 > 3 個百分點的判定放在 Finding 的 `detail`（`exact_pct`、`major_pct`），退步比較由 CLI 的 D2 專用規則處理（見 Task 6），本 task 只負責算出百分比。D3 分母與不符數皆排除 `no_box`。
- Produces（`checks_lines.py`）：`check_e1_line_coverage`、`check_e2_spread_range`、`check_e3_spread_health`、`check_e4_total_range`、`check_e5_total_health`、`check_e6_price_point_provenance`、`check_e7_line_flip`。盤口一律取自 `public.v_closing_lines`。E3 另算 `avg(margin+home_spread)` 並對 [−1.0, +1.0] 判定；比例型檢查可比對 < 200 只回報。E4 範圍含**所有** season_type，並排除 NULL 另記 info。E5 先排除 E4 旗標的場次再算相關。
- [ ] **Step 1: 寫失敗測試**：`test_shots_class` 全部分類與邊界（差 2 → minor、差 3 → major、box None → no_box）；D2 分母排除 no_box（用 2015-16 風格資料：2460 隊場含 322 NULL，exact 1115 → 52.2%）；D2 n<200 為 info；D3 排除 no_box；E2 對 `abs(spread)>25`；E3 相關在範圍內 ok、0.9 → error；E3 漂移 +1.2 → error；E4 對 2001、−1、49.5 都旗標，且 2021-22 的非 regular 場也在範圍內；E5 排除 E4 旗標後才算；E6 出現 `captured_at ≠ tipoff` 的 NULL 列為 warn；E7 負相關為 error；SQL 只讀掃描。
- [ ] **Step 2: 跑** → FAIL。**Step 3: 實作。** **Step 4: 跑** `python -m pytest tests -q` → PASS。
- [ ] **Step 5: Commit** `git commit -am "Add shot-bucket and betting-line quality checks"`（含新檔）

---

### Task 5: `game_quality()` 每場品質旗標

**Files:**
- Create: `jobs/quality/game_quality.py`、`tests/test_quality_game_quality.py`

**Interfaces:**
- Produces：`game_quality(run: Run, scope: Scope) -> pd.DataFrame`，欄位與型別完全依設計 §5 表格（`game_id`…`usable_total_line`），每場一列；一段 SELECT（CTE）；SQL 開頭 `/* qc:GQ */`。
- Produces（純 Python 對照實作，供測試與 CLI 抽樣核對）：`derive_flags(row: dict) -> dict`：輸入一場的原始計數（兩隊 box 欄位非 NULL 數、球員分鐘非 NULL 列數、球員得分加總、球隊得分、桶 FGA、先發數、裁判數、盤口值與 provenance 等），輸出設計 §5 的 `team_box_ok … usable_total_line` 布林欄位。SQL 與 Python 兩邊的判斷必須一致（見測試）。
- 常數：`CUP_FINAL_IDS = {"401607495", "401734908", "401809839"}`；`BUBBLE_START = date(2020, 7, 30)`、`BUBBLE_END = date(2020, 10, 12)`；`CROWD_RESTRICTED = (date(2020, 12, 22), date(2021, 5, 31))`。
- [ ] **Step 1: 寫失敗測試**：`test_derive_flags_*`（佔位 → `team_box_ok` False 且 `usable_player_features` False；得分不一致；`shots_class` minor → `usable_shot_features` True、major → False；總分線 2001 → `usable_total_line` False；provenance NULL 且 captured_at＝tipoff → `backfill_unlabeled`；缺一隊 team 列 → `team_box_ok` False）；`test_cup_final_and_bubble_flags`；`test_game_quality_sql_is_select_only_and_has_all_columns`（用 FakeDb 路由 `qc:GQ` 回一個假 DataFrame，斷言函式原樣回傳且欄位集合等於設計 §5 的欄位集合）。
- [ ] **Step 2: 跑** → FAIL。**Step 3: 實作。** **Step 4: 跑** `python -m pytest tests -q` → PASS。
- [ ] **Step 5: Commit** `git commit -am "Add per-game quality flags (read-only function)"`（含新檔）

---

### Task 6: CLI、A1（ESPN 覆蓋）、D2 退步判定

**Files:**
- Create: `jobs/quality/runner.py`、`jobs/quality/__main__.py`、`jobs/quality/checks_espn.py`、`tests/test_quality_runner.py`
- Modify: `jobs/quality/__init__.py`（匯出 `run`、`REGISTRY`）

**Interfaces:**
- Produces（`runner.py`）：
  - `REGISTRY: list[tuple[str, Callable]]`：A2–A7、B1–B6、C1–C3、D1–D4、E1–E7 的 `(id, fn)`；A1 另列。
  - `run(run_sql: Run, scope: Scope, baseline: dict, *, with_espn: bool = False, fetch_scoreboard=None) -> list[Finding]`：依序執行所有檢查，對每個 Finding 呼叫 `classify(..., since_mode=scope.since is not None)`。D2 的狀態改用專用規則：`exact_pct` 比基準線記錄的 `exact_pct` 低超過 3 個百分點才 `worse`，否則 `known`；基準線 entry 為 D2 時額外存 `exact_pct`。
  - `main(argv: list[str] | None = None) -> int`：參數 `--seasons a,b`／`--all`（二選一，預設 `--all`）、`--since 7d`、`--with-espn`、`--fail-on {error,warn,none}`（預設 `warn`）、`--json PATH`、`--baseline PATH`（預設 `jobs/quality/baseline.json`）、`--write-baseline PATH`（把目前所有 `n_bad > 0` 的 Finding 寫成基準線檔，`as_of_date` 為執行當天；**僅寫本機檔案，不碰 DB**）。`--since` 時只評估 `game_date_et > baseline.as_of_date` 的場次；無新場次輸出「無新資料」且結束碼 0。連線用 `connect_readonly()`；任何例外只印例外類別與固定訊息，不印連線字串。
- Produces（`checks_espn.py`）：`check_a1_espn_event_coverage(run, scope, fetch_scoreboard) -> list[Finding]`：對 scope 內每個 ET 日期（`--since` 時最近 N 天）呼叫 `fetch_scoreboard(yyyymmdd)`，數「NBA 對 NBA 已完賽」事件，與 DB 同日同賽別場數比；被略過的事件類型不在白名單（全明星賽、季前賽非 NBA 隊、`TBD`）即 error。`fetch_scoreboard` 預設為 `jobs.espn_http.scoreboard`；只在 `--with-espn` 時執行。
- [ ] **Step 1: 寫失敗測試**：`test_run_applies_baseline_states`（假檢查結果＋基準線）、`test_d2_regression_threshold`（退步 3.0 個百分點仍 known、3.1 → worse）、`test_since_mode_reports_no_new_data`、`test_main_exit_codes`（monkeypatch `connect_readonly`、`run`）、`test_main_does_not_leak_dsn`（`connect_readonly` 拋出含 `postgresql://u:secret@h/db` 的 RuntimeError → capsys 無 `secret`）、`test_write_baseline_writes_file_only`（斷言輸出檔存在、且過程沒有 `execute` 含寫入關鍵字）、`test_a1_detects_missing_game_and_unknown_skip_type`（假 scoreboard：多一場 NBA 對 NBA → error；白名單外的縮寫 → error；全明星賽 → 不報）、`test_a1_skipped_without_flag`。
- [ ] **Step 2: 跑** → FAIL。**Step 3: 實作。** **Step 4: 跑** `python -m pytest tests -q` → PASS。
- [ ] **Step 5: Commit** `git commit -am "Add quality CLI, ESPN coverage check and baseline-aware runner"`（含新檔）

---

### Task 7: 文件、對正式 DB 唯讀驗證、審查

**Files:**
- Modify: `CLAUDE.md`（指令：`python -m jobs.quality`；架構：`jobs/quality/` 只讀；不能違反的規則：`jobs/quality` 只准 SELECT、基準線修改須說明）、`docs/handoff/2026-10-23-roadmap.md`（P3a 狀態）
- Create: `jobs/quality/baseline.json`（Step 3 產生）

- [ ] **Step 1:** 更新文件並 commit `git commit -am "Document the quality checks"`。
- [ ] **Step 2:** 貼出 `python -m pytest tests -q`、`git log --oneline -- tests/golden`（只有建立那一個 commit）、`git diff main..HEAD --stat`（無 `jobs/schema.py`、無 `.github/` 變動）。
- [ ] **Step 3（交主線）**：主線在本機用唯讀連線執行 `python -m jobs.quality --all --write-baseline jobs/quality/baseline.json`，對照設計 §3／§8 與 `docs/handoff/p3-sql-results-2026-10-25.md` 逐項比對；**不一致的項目必須找出原因，不能直接採用較大的數字**。通過後把 `baseline.json` 連同比對結果 commit：`git commit -am "Add the verified quality baseline"`。這一步只讀 DB（`connect_readonly`），不需使用者同意。
- [ ] **Step 4:** 交 `reviewer`（Opus）審 `git diff main..HEAD`。prompt 重述：全模組只讀、不得放寬門檻、基準線只能由實測產生、不改 workflow 與 schema、golden 不變、不可 push。修完 Critical／High 回報主線。

---

## 第 7 章：需要使用者同意（由主線執行）

1. push `p3-data-quality`，`main` 快轉並 push（內容只有文件、`jobs/quality/`、測試與基準線，不影響每日排程與網站）。
2. P3b（另寫計畫）：workflow 接線（`v2_quality` dispatch、每日品質 step 失敗讓 CI 變紅）— 會改 `daily_nba.yml`，屬 workflow 變更，合併後立即影響每日排程，要先取得使用者同意。
