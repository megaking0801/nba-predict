# 設計：傷兵「第一次看到」事件表＋事件觸發 ESPN 盤口快照（第一階段，2026-10-28）

狀態：**設計，未實作**。所屬實驗見 `docs/handoff/2026-10-28-prematch-info-evaluation.md` (b)。
動到 schema、排程或寫正式 DB 之前，依 CLAUDE.md 要主線與使用者先審。

## 0. 先講結論：Actions 排程做不到分鐘級
- 實驗的判斷條件之一是「落後窗口中位數 ≥ 60 分鐘」，量這個至少要 **5～10 分鐘**的偵測與快照解析度。
- GitHub Actions 的 `schedule` 在此 repo 實測晚 3～4 小時才開跑（`daily_nba.yml` 註解：10:30 UTC 的 cron 實際在 14:05、14:32 UTC 開跑），啟動時間不可預期，事件觸發的「+5、+15 分鐘」快照根本排不進去。
- 所以**只靠 `daily_nba.yml` 的 cron，這個實驗做不了**。用它最多得到「幾小時粒度」的資料，只能回答「落後是否超過數小時」，回答不了「是否至少 60 分鐘」，也量不出真正的偵測延遲。
- 可行的做法（要使用者選，細節見 §5）：另一個能每 1～5 分鐘準時執行的環境。我查了 DB（只讀）：`pg_cron 1.6.4`、`pg_net 0.20.3`、`http 1.6` **可用但尚未安裝**（`pg_available_extensions`），所以「資料庫自己排程」是現成選項之一。是否允許安裝擴充套件、方案是否支援，**未驗證**。

## 1. 要量什麼（與評估文件對齊）
事件 E＝（隊、球員）的傷兵狀態出現變化；之後 60 分鐘內，ESPN 盤口（`market_lines`，`book='espn'`）有沒有動、動多少。第一階段只回答：
1. 偵測延遲：事件的 `reported_at`（ESPN 說的更新時間）到我們 `first_seen_at` 差多久（需要 ≤ 5 分鐘的輪詢才有意義）。
2. ESPN 盤口自己的更新頻率與在事件後的反應（動不動、多久動、動幾分）。
3. 事件頻率（每天幾個關鍵事件），用來決定值不值得付國際盤的錢。

**混淆**：ESPN 盤口是 ESPN 從供應商取得的單一來源，它的「慢」可能是供應商或 ESPN 的刷新週期，不是莊家的反應速度。第一階段不能因此推論台彩。

## 2. Schema（新表，可重複執行、只新增）
```sql
CREATE TABLE IF NOT EXISTS public.injury_events (
  event_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  first_seen_at   TIMESTAMPTZ NOT NULL,           -- when WE first saw this (team, player, status, reported_at)
  reported_at     TIMESTAMPTZ,                    -- ESPN's own timestamp for the entry (may be NULL)
  team_abbr       TEXT NOT NULL REFERENCES public.teams(team_abbr),
  player_name     TEXT NOT NULL,
  espn_player_id  BIGINT,                         -- NULL on rows seen before the 2026-10-28 parser fix
  kind            TEXT NOT NULL,                  -- 'new' | 'status_change' | 'update' | 'cleared'
  status          TEXT,                           -- NULL for 'cleared'
  prev_status     TEXT,
  detail          TEXT,
  next_game_id    TEXT REFERENCES public.games_v2(game_id),  -- team's next game within 36 h of first_seen_at
  UNIQUE (team_abbr, player_name, kind, status, reported_at)
);
CREATE INDEX IF NOT EXISTS idx_injevt_seen ON public.injury_events (first_seen_at);
CREATE INDEX IF NOT EXISTS idx_injevt_game ON public.injury_events (next_game_id);
```
- 事件才寫，不是每次抓取都寫。對照：`injury_reports` 若改成每 5 分鐘 append 全部約 90 列，一個月約 45 萬列；事件表以 2026-10-25 起的實際變化量估算約每天 10 來個列，幾乎沒有儲存成本。（目前 DB 211 MB；`injury_reports` 336 kB、`market_lines` 3.4 MB。）
- `UNIQUE (…)` 讓重複執行安全（`INSERT … ON CONFLICT DO NOTHING`）。`reported_at` 是 NULL 時，PostgreSQL 的 UNIQUE 不視為重複，需在程式裡用最新一筆比對狀態來避免重複寫入，不要只靠約束。
- 不改 `market_lines`（有欄位順序與 view 的限制）。事件與盤口在分析時用 `game_id` ＋ `captured_at` 區間 join，不另設關聯表。

偵測規則（純函式，可單測）：取每個（隊、球員）最近一筆事件為「已知狀態」，與這次抓到的條目比較：
- 沒有已知狀態且條目存在 → `new`；
- 狀態不同 → `status_change`（`prev_status` 填已知狀態）；
- 狀態相同但 `reported_at` 變新 → `update`（教練說法變了，常是訊號）；
- 先前存在、這次條目消失 → `cleared`（球員復出或被移除；對 Out→可上場很重要）。
- 球員識別一律用（`team_abbr`, `player_name`）；`espn_player_id` 只是附帶欄位，因為歷史列是 NULL，且改名／重複名字的風險很小。

## 3. 快照觸發（事件觸發式盤口）
- 每個輪詢週期（§4）：抓 ESPN `injuries` → 偵測 → 寫事件。
- **盤口取樣規則**：任一場「開賽時間在未來 14 小時內、且其中一隊在最近 60 分鐘內有事件」的比賽，每個輪詢週期都抓一次 ESPN 盤口（沿用 `snapshot_espn_odds` 的抓法與 `INSERT_LINE`）。60 分鐘後降回每 30 分鐘一次，到開賽前 150 分鐘併入既有 close 流程。
- 沒有事件的比賽維持現狀（每天一批＋close），不增加請求量。
- 預估請求量：每天約 10 個事件、關鍵事件約 3 個；每個窗口 12 次盤口請求 → 每天幾十次。ESPN 這個 endpoint 現有程式已在使用；5 分鐘一次的 injuries 請求（每天約 170 次）遠高於現在（約 4 次），對方是否限流或有條款限制**未驗證**，先用 10 分鐘試跑，沒問題再縮短。

## 4. 輪詢環境：誠實比較
| 選項 | 解析度 | 成本／風險 | 判斷 |
| :- | :- | :- | :- |
| `daily_nba.yml` 的 cron | 小時級、起點不可預期 | 無額外成本 | **不夠，不採用** |
| Actions 長時間 job（cron 啟動後在 job 內每 5 分鐘迴圈，最長跑數小時） | 起點仍晚 3～4 小時，錯過 ET 上午到午後的窗口；job 時間上限與重試行為未驗證 | 免費額度未驗證（public repo）；把 Actions 當常駐輪詢器是否符合 GitHub 條款**未驗證**，有被限制的風險 | 不建議 |
| 資料庫排程：`pg_cron`＋`pg_net`（每分鐘 tick，SQL 呼叫 ESPN、直接寫表） | 1 分鐘 | 要在正式 DB 安裝擴充套件、邏輯寫在 SQL（難測、難版本管理）；擴充套件的可用性已確認存在，**能否啟用與方案限制未驗證** | 可行，但把排程與抓取邏輯放進正式 DB，我不偏好 |
| 使用者自己的電腦（Windows 工作排程器，每 5 分鐘跑 Python） | 5 分鐘 | 需開機整夜（ET 10:00～22:30＝台北 22:00～10:30）；要寫正式 DB 的憑證放在本機 | 能最快驗證，但不穩定 |
| 小型雲端 VM 或 Cloudflare Workers cron 觸發 | 1 分鐘 | 小額月費或免費額度（**價格未查證**）；多一個要維運的服務與密鑰 | 長期最穩，需使用者決定 |

我的建議是**分兩步，先不花錢**：
1. **階段 0（不寫 DB、不花錢）**：寫一個本機輪詢腳本，只做兩件事——每 5 分鐘 GET 一次 ESPN `injuries` 與當天各場的盤口，把原始回應的變化以 JSONL.gz 存到 `data/`（資料 repo，依 CLAUDE.md 規則；單檔超過約 10 MB 先問）。跑 3～5 天，用來量：ESPN 盤口自己多久會動、事件率、`reported_at` 與偵測的關係。這一步不動 production，使用者的電腦開著即可。
2. **階段 1**：階段 0 顯示「盤口確實會動、事件率 ≥ 約 2 個關鍵事件／天」才選上表其中一個環境，把 §2 的表與 §3 的規則正式上線（此時才需要使用者同意寫正式 DB）。階段 0 若顯示 ESPN 盤口一天只動 1～2 次，這條路的結論就是「ESPN 盤口不是即時來源，無法當代理」，實驗在此結束，省下後面所有成本。

## 5. 要使用者決定的事（我不會自己做）
1. 是否執行階段 0（本機長時間輪詢，只讀 ESPN、寫到 `data/`）。
2. 若繼續：選哪個輪詢環境（§4），以及是否同意寫正式 DB、安裝 `pg_cron`/`pg_net`、或付費服務。
3. 是否申請 Odds API 方案（花錢；方案與點數規則未查證）。國際盤是 (b) 的參考線，沒有它就只能看 ESPN 盤口自己的反應。

## 6. 程式與測試（階段 1 才寫）
- `jobs/injury_events.py`：`detect(known, current) -> events`（純函式）、`next_game_for(team, ts, games)`；單元測試覆蓋 new／status_change／update／cleared／重複輸入冪等／`reported_at` 為 NULL。
- 輪詢入口 `python -m jobs.injury_poll --interval 300`（階段 0 版本只寫檔、不連 DB）。
- `ensure_schema` 加入上面的 DDL（可重複執行）；`jobs/quality` 不碰這張表。
- 不新增依賴；不改 `daily_nba.yml`。

## 7. 與修正 `espn_player_id` 的關係
- 2026-10-28 的修正（`jobs/snapshot_injuries.py` `_player_id`）只影響之後的抓取；既有 1,552 列（`injury_reports`）與舊表 `injury_snapshots` 的 NULL 不會自動補。
- 回補需要寫正式 DB：可用 `roster_snapshots` 以（`team_abbr`, `player_name`）對出 `espn_player_id`。改名、同名球員要先查。**需要使用者同意，未做。** 事件表用名字識別，不依賴回補。
