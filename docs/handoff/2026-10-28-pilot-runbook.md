# 階段 0 輪詢腳本：怎麼跑（2026-10-28）

腳本：`jobs/pilot_poll.py`（偵測邏輯在 `jobs/injury_events.py`）。設計與判斷標準見 `docs/superpowers/specs/2026-10-28-injury-events-design.md` 與 `2026-10-28-prematch-info-evaluation.md`。
**它不會自己啟動；要使用者準備好環境並同意後才跑。** 只做 GET，不寫 DB，輸出寫在 `data/pilot/`（資料 repo）。

## 它做什麼
- 每 5 分鐘（可調，最短 60 秒）GET ESPN 傷兵報告，和上一輪比較，狀態變成 Out／Day-To-Day 且該隊 14 小時內有比賽 → 事件。
- 事件發生後 60 分鐘內，每輪 GET 該場的 ESPN 盤口；另外每 30 分鐘對所有 14 小時內的比賽取一次 ESPN 盤口基準。只存「有變化」的盤口。
- 若有 `ODDS_API_KEY`：事件後 +0、+5、+15、+30、+60 分鐘各取一次 The Odds API（一次呼叫回全部比賽，1 點），並每 3 小時取一次基準樣本（當天用量不到上限一半時）。每家莊家自己的 `last_update` 和我們的抓取時間 `fetched_at` 一起存。
- 點數硬上限：每天 16、每月 450、並保留 30 點（看廠商回傳的剩餘點數）。狀態存在 `data/pilot/pilot-state.json`，重啟不會重算。

## 使用者要做的事
1. 自己到 https://the-odds-api.com 註冊免費金鑰（不需信用卡，據第三方頁面；**金鑰不要貼到任何對話或檔案**）。
2. 在 PowerShell 設環境變數（只在這個視窗有效）：`$env:ODDS_API_KEY = "<你的金鑰>"`。不設金鑰也能跑，只是不取 Odds API。
3. 先試一輪確認連得上：`python -m jobs.pilot_poll --once`（會連 ESPN；網路問題只會看到 WARN，不會當掉）。
4. 正式跑：`python -m jobs.pilot_poll --interval 300 --max-hours 14`。電腦要保持開機不睡眠；ET 上午到最後一場開賽＝台北約 22:00 到隔天 10:00。Ctrl+C 可隨時停。

## 輸出
`data/pilot/poll-YYYY-MM-DD.jsonl.gz`（UTC 日期），每行一筆 JSON，`src` 欄位為：`injury_event`（含 `baseline`）、`espn_line`、`oddsapi`、`heartbeat`、`error`。
用 `gzip.open(path, "rt")` 逐行讀。每天預估數十 KB 壓縮後（只存變化＋每輪一筆小心跳），遠低於資料 repo 規定的約 10 MB 提示線。**commit／push 資料 repo 需使用者同意。**

## 注意
- ESPN 端點與 Disney 條款的差距見 `2026-10-28-data-source-survey.md` 開頭；是否執行由使用者決定。
- 使用者在台灣：ESPN 這幾個端點目前的 GitHub 排程在用，從家裡連通常也可以，但沒有實測過；失敗時看 `error` 記錄。
- 事件包含所有球員；「關鍵球員」（近 10 場分鐘前 7 名）在事後分析時用 DB 的 `player_game_stats` 篩，這支腳本不連 DB。
- 分析腳本（事件 × 盤口時間序列 → 判斷標準）還沒寫，要等有資料。
