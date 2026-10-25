# P3b 品質檢查接上每日排程 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `daily_nba.yml` 新增手動 `v2_quality` 與每日（10:30 UTC 之後）的獨立品質檢查 step；有 new／worse 時 CI 變紅，且能與資料抓取的紅燈分辨。

**Architecture:** 只改 `.github/workflows/daily_nba.yml` 與 `tests/test_daily_workflow.py`（文字型測試，不 import yaml）。品質檢查程式（`jobs/quality`）與基準線已在 `main`，本計畫不改。

**Tech Stack:** GitHub Actions、pytest。不新增依賴。

**Spec:** `docs/superpowers/specs/2026-10-25-p3-data-quality-design.md` §7.4、§9（P3b）。

## Global Constraints

- 遵守 `CLAUDE.md`。分支：從 `main` 開 `p3b-quality-workflow`。
- 品質 step 必須是獨立 step，**不併進 `run_daily` 的步驟清單**（兩種紅燈要能分辨）；名稱固定 `v2 quality (read-only data checks)`。
- 每日 step：`if: ${{ always() && github.event.schedule == '30 10 * * *' }}`，指令 `python -u -m jobs.quality --since 7d --fail-on warn`。收盤快照的三個 cron（`30 22`、`30 0`、`0 2`）與週一重訓**不**跑品質檢查。
- 手動 step：`inputs.run == 'v2_quality'`，指令 `python -u -m jobs.quality --all --fail-on warn`（完整執行，用基準線比對）。
- 不新增 secrets、不新增權限（沿用 job 層既有 env）；不改 `site.yml`、不改 `jobs/`。
- 測試只做文字比對，每個 task 結束跑 `python -m pytest tests -q` 並貼輸出。commit 訊息英文，結尾 `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`。
- **不 push、不觸發 workflow**，直到第 7 章由主線取得使用者同意。

## Review Focus

1. 資料抓取失敗（`run_daily` 退出碼 1）時，品質 step 仍要執行（`always()`），且兩個 step 的紅燈在 Actions 步驟列表中各自可見。→ Task 1。
2. 品質 step 不得出現在收盤快照或重訓的條件裡（否則一天跑四次、多出紅燈）。→ Task 1 的測試。
3. 手動 `v2_quality` 的說明字串要列在 `inputs.run` 的 description，否則使用者不知道有這個選項。→ Task 1。
4. 品質 step 不可帶 `--write-baseline`（那會在 CI 上改檔案）。→ Task 1 的測試。

---

### Task 1: workflow step 與測試

**Files:**
- Modify: `.github/workflows/daily_nba.yml`、`tests/test_daily_workflow.py`

**Interfaces:**
- Consumes: `python -m jobs.quality` 的 CLI（`--since Nd`、`--all`、`--fail-on`、`--baseline`）。
- Produces（`daily_nba.yml`）：
  - `inputs.run` 的 description 在 `v2_evaluate` 之後加上 `| v2_quality`。
  - 在 `v2 daily pipeline` step **之後**插入兩個 step：
    - `v2 quality (read-only data checks)`：`if: ${{ always() && github.event.schedule == '30 10 * * *' }}`，`run: python -u -m jobs.quality --since 7d --fail-on warn`。
    - `v2 quality (manual, full run)`：`if: ${{ github.event_name == 'workflow_dispatch' && inputs.run == 'v2_quality' }}`，`run: python -u -m jobs.quality --all --fail-on warn`。

- [ ] **Step 1: 寫失敗測試**（`tests/test_daily_workflow.py`，沿用該檔既有的 step 區塊切法）：`test_quality_step_runs_after_daily_pipeline_even_on_failure`（該 step 存在、位置在 `v2 daily pipeline` 之後、`if` 含 `always()` 與 `'30 10 * * *'`）、`test_quality_step_command_and_fail_on`（含 `jobs.quality --since 7d --fail-on warn`，且不含 `--write-baseline`）、`test_quality_not_on_close_or_train_schedules`（兩個品質 step 的 `if` 都不含 `'30 22 * * *'`、`'30 0 * * *'`、`'0 2 * * *'`、`'0 12 * * 1'`）、`test_quality_manual_dispatch`（`v2_quality` 出現在 input description；手動 step 的指令含 `--all --fail-on warn` 且不含 `--write-baseline`）、`test_quality_is_not_part_of_run_daily`（`jobs/run_daily.py` 原始碼不含 `quality`）。
- [ ] **Step 2: 跑** `python -m pytest tests/test_daily_workflow.py -q` → FAIL。
- [ ] **Step 3: 修改 workflow。** 只加上述兩個 step 與 description 字串，其他行不動。
- [ ] **Step 4: 跑** `python -m pytest tests -q` → PASS。
- [ ] **Step 5: Commit** `git commit -am "Run the read-only quality checks after the daily pipeline and on demand"`

---

### Task 2: CLAUDE.md 與交接文件

**Files:**
- Modify: `CLAUDE.md`（排程一行補「10:30 UTC 之後另有獨立的品質檢查 step，有新問題會讓 CI 變紅，與資料抓取的紅燈分開」）、`docs/handoff/2026-10-23-roadmap.md`（P3b 狀態）

- [ ] **Step 1:** 更新文件並 commit `git commit -am "Document the quality workflow step"`。
- [ ] **Step 2:** 貼出 `python -m pytest tests -q`、`git diff main..HEAD --stat`（只能有 `.github/workflows/daily_nba.yml`、`tests/test_daily_workflow.py`、`CLAUDE.md`、`docs/`）。

---

## 第 7 章：需要使用者同意（主線執行）

這一章改變正式排程的行為，逐項取得同意：

1. **試跑（只讀）**：使用者用 `!` 執行輔助腳本 `run_quality.py --since 7d --fail-on warn`，確認目前實際結果（預期結束碼 0；若有 new／worse，先判讀是資料問題還是基準線問題再往下）。
2. push `p3b-quality-workflow`，快轉 `main` 並 push（合併後下一次 10:30 UTC 排程就會執行品質 step）。
3. 手動觸發一次 `daily_nba` 的 `v2_quality`（完整、只讀），確認 CI 上結束碼 0、`jobs.quality` 在 Actions 環境可連線。
4. 觀察第一次 10:30 UTC 排程的兩個 step：資料抓取與品質檢查各自的結果。
