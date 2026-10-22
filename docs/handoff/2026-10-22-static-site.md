# 交接：靜態網站（一直在線）

日期：2026-10-22
下一個 session 先讀這份，再讀 `CLAUDE.md` 與規格檔。

## 一句話現況
使用者要讓網站「一直在線」。已完成調查與設計，規格與實作計畫已完成並納入兩輪審查；**程式碼已在本機分支 `static-site` 實作完成（尚未 push、未啟用 Pages、未觸發任何 workflow）**。見文末「2026-10-23 實作狀態」。

## 工作目錄
- 在 `C:\Users\king\Documents\nba-predict` 開 Claude Code（這個資料夾才有 git 與 `.claude/`，CLAUDE.md 才會載入）。
- `C:\Users\king\Documents\nba-predict-main` 是舊的 zip 下載，內容與 `main` 相同，不是 git repo，不要在那裡工作。
- `.streamlit/secrets.toml` 已複製到新資料夾，未被追蹤，也被 deny 規則擋住讀取。
- 新開 session 後執行 `/context` 確認 CLAUDE.md 有載入，`/hooks` 確認 Stop hook 在列表上，再跑一次 `/doctor prompt-audit`。

## 這個 session 做了什麼
1. clone `megaking0801/nba-predict`；確認內容與舊 zip 相同（只差換行符號）。
2. `.gitignore` 加入 `.streamlit/secrets.toml`、`.claude/settings.local.json`、`CLAUDE.local.md`。
3. 新增 `CLAUDE.md`（89 行）、`.claude/agents/`（explorer／builder／reviewer）、`.claude/settings.json`（secrets deny + Stop hook）、`.claude/hooks/pytest_gate.py`。Stop hook 的四種情況都實測過。
4. 跑測試：26 個全過（約 2 秒）。
5. 調查「為什麼不能一直在線」（見下），與使用者一起決定做靜態網站。
6. 寫規格：`docs/superpowers/specs/2026-10-21-static-site-design.md`。

## 調查結果（事實）
- 網站部署在 Streamlit Community Cloud，**沒流量 12 小時就睡**，官方沒有常駐選項。
- `daily_nba` workflow 狀態是 `disabled_inactivity`，最後排程執行 2026-08-19（公開 repo 60 天沒活動會被停用）。
- 最近 40 次執行 21 次失敗：10:30 UTC 全流程中 `ingest_games` 被 `cdn.nba.com` 回 403，`ingest_stats` 打 `stats.nba.com` 逾時。成功的 22:00 UTC 那次只做抓盤與預測，所以看起來綠燈。
- `ingest_espn.py` 已存在，但沒接進 `run_daily`。
- 使用者沒有提供網站網址（選了靜態方案後就不需要了）。

## 已定案的決定
| 項目 | 決定 |
| :- | :- |
| 託管 | GitHub Pages（Actions 部署） |
| 第一版範圍 | 與 Streamlit 完全對等（看板、單場詳情、30 個球隊頁、模型戰績） |
| 實作 | 抽出共用渲染模組 `webui/render.py`、`webui/queries.py`，新增 `webui/build_site.py`，Streamlit 改為 import |
| 資料新鮮度 | 每頁顯示更新時間，超過 36 小時顯示「資料可能已過期」 |
| 建置與抓資料 | 分離：`daily_nba` 完成後不論成功或失敗都重建 |
| Streamlit | 暫時保留，行為不變 |

## 下一步（照順序）
1. **等使用者審閱規格**，有意見就改。
2. 使用者同意後，用 `superpowers:writing-plans` 寫實作計畫，請使用者選執行方式。
3. 實作順序見規格第 9 節：先寫特徵化測試鎖住現有輸出，再抽出模組，再寫建置腳本與 workflow。
4. 實作完成後交給 `reviewer`（opus）審 diff。

## 需要使用者明確同意才能做的事
- 把 Pages 來源改成 GitHub Actions（repo 設定變更）。
- 手動觸發 workflow、`gh workflow enable daily_nba`。
- 任何寫入正式 Supabase 的 job。

## 第二個子專案（尚未開始，緊接著做）
恢復排程與抓資料，否則網站內容會停在舊資料：
- 重新啟用 `daily_nba`（`gh workflow enable daily_nba`）。
- 把 ESPN 抓取（`ingest_espn.py`）接進 `run_daily`，取代被擋的 `ingest_games` / `ingest_stats`。
- 處理 60 天無活動會停用排程的問題。
- 新球季 2026-27 已開打，確認賽程與球季字串能正確寫入。

## 尚未驗證的事
- 部署失敗時，線上是否維持上一版（文件沒明說，規格第 9 節第 5 步要實測）。
- GitHub 文件沒有逐項列舉哪些算「repo 活動」。
- Cloudflare Pages 的免費額度（第三方整理，已因選 GitHub Pages 而不需要）。
- `deny` 規則在新 session 的實際效果沒有實測。

## 其他注意
- 大小分方向命中率約 48%（walk-forward，來自舊記憶），App 以估總分為主並附警語，維持這個誠實立場。
- Legacy 流程（`public.games`、`sync_daily` 等）仍在，不要擴充。
- commit 訊息用英文；回覆用繁體中文。

## 2026-10-23 清理
- 舊資料夾 `nba-predict-main` 已於 2026-10-23 01:05 移到資源回收筒（可還原）；舊 session 紀錄 `~\.claude\projects\c--Users-king-Documents-nba-predict-main` 保留。
- 新 session 確認：CLAUDE.md、memory（`...nba-predict\memory`）、explorer／builder／reviewer 有載入，`settings.json` 有 Stop hook；`/hooks` 實際列表未由使用者確認。

## 2026-10-23 實作狀態
- 計畫：`docs/superpowers/plans/2026-10-23-static-site.md`（9 個 task，1–8 已完成並 commit 在 `static-site` 分支）。
- 新增：`webui/render.py`、`webui/queries.py`、`webui/build_site.py`、`.github/workflows/site.yml`、`tests/fakes.py`、整頁 golden（`tests/golden/`）與各項測試。
- 使用者授權兩個 session 自行決定細節；**以下仍須使用者逐項明確同意**：`git push`、把 Pages 來源改為 GitHub Actions、手動觸發 `site` workflow、`fail_build=true` 實測失敗不覆蓋線上。
- 已驗證（假資料）：用真的 `streamlit run`（DB 換成 `tests/fakes.py` 的假資料）啟動並以 headless Edge 截圖，看板與單場頁正常；靜態站在 `/nba-predict/` 子路徑下爬 35 頁無壞連結，並以 headless Edge 截圖確認桌面與手機寬度版面（截圖中發現容器沒有置中與邊距，已修）；3.11 語法以 parso 3.11 文法檢查 14 個檔案無錯。
- 仍未驗證：連真實 DB 的 `streamlit run` 與 `build_site`；CI 實跑 Python 3.11（只做了語法層檢查）；部署失敗時線上是否維持上一版；Pages 實際網址。
- 已知行為：頁面改為查完全部資料才一次輸出，查詢出錯時不再先顯示部分頁面。
- 第一次部署順序：`site.yml` 進 `main` 就會因 push 觸發；若 Pages 來源還沒改成 GitHub Actions，`deploy` 會失敗一次，所以要先啟用 Pages 再 push（或接受第一次失敗）。CI 的 pytest 以 `pip install pytest==9.1.1` 在 workflow 內安裝，沒有加進 `requirements.txt`。
- 提醒：`daily_nba` 仍停用，靜態站內容會停在最後資料，約 36 小時後顯示「資料可能已過期」。
