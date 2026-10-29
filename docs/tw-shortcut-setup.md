# iPhone 捷徑：台彩賽前盤自動快照（設定步驟）

設計見 `docs/superpowers/specs/2026-10-29-tw-snapshot-design.md`。
這個捷徑每天在 7 個時段，把台彩籃球賽前盤、ESPN 計分板（國際盤）、The Odds API（Pinnacle 等）的原始回應，上傳到 **private** 資料 repo `megaking0801/nba-predict-data` 的 `tw_snapshots/` 底下。
程式 repo（public）不會被動到。手機必須在台灣（台彩資料流只有台灣 IP 連得到）。

**金鑰與權杖只存在手機的捷徑裡，不要貼到對話、不要存進任何 repo。**

---

## 0. 事前準備（約 10 分鐘）

### 0.1 建立 GitHub 權杖（只限資料 repo）
1. 在瀏覽器登入 GitHub → 右上頭像 → **Settings** → 左欄最下面 **Developer settings**。
2. **Personal access tokens** → **Fine-grained tokens** → **Generate new token**。
3. 填寫：
   - Token name：`tw-snapshot-shortcut`
   - Expiration：選最長（1 年左右）。到期前一週 GitHub 會寄信提醒。
   - Repository access：**Only select repositories** → 只勾 `megaking0801/nba-predict-data`。
   - Permissions → Repository permissions → **Contents：Read and write**（其他全部保持 No access；Metadata 會自動變成 Read-only，正常）。
4. 按 **Generate token**，複製那串 `github_pat_…`。這是唯一一次能看到它，等一下直接貼進捷徑。

### 0.2 The Odds API 金鑰
用你原本那把。沒有的話到 https://the-odds-api.com 註冊免費方案（每月 500 點），信裡會有金鑰。這個捷徑每月約用 360 點。

---

## 1. 建立捷徑「台彩快照」

打開 **捷徑** App → 右上 **＋** → 名稱改成 `台彩快照`。依序加入下面的動作（上方搜尋框打中文動作名稱就找得到）。
「變數」是指動作輸出後，用「設定變數」存起來的名字；之後的動作裡點欄位 → 選那個變數。

### 1.1 時間與日期
1. **目前日期**。
2. **格式化日期**：日期 = 目前日期；日期格式 = **自訂**，格式字串 `yyyyMMdd'T'HHmmZ`（例：`20261029T2100+0800`）。→ **設定變數** `時間戳`。
3. **格式化日期**：日期 = 目前日期；自訂格式 `yyyyMMdd`。→ **設定變數** `資料夾`。
4. **格式化日期**：日期 = 目前日期；自訂格式 `H`（只有小時，0～23）。→ **設定變數** `小時`。
5. **調整日期**：目前日期 **減** `12` **小時** → **格式化日期**：調整後的日期，自訂 `yyyyMMdd` → **設定變數** `美東日期`。

（減 12 小時 = 「台北 ≥ 12 點用當天，否則用前一天」：台北 21:00 → 美東同一天；台北早上 06～11 點 → 美東前一天。ESPN 要用美東日期。）

### 1.2 台彩
6. **取得 URL 內容**：URL `https://blob3rd.sportslottery.com.tw/apidata/Pre/34765.1-Games.zh.json`；方法 **GET**；不要加任何標頭。
7. **Base64 編碼**：輸入 = 上一步的內容；**換行：無**（預設是每 76 個字元換行，一定要改成「無」）。
8. **取得 URL 內容**（上傳）：
   - URL：`https://api.github.com/repos/megaking0801/nba-predict-data/contents/tw_snapshots/` + `資料夾` + `/` + `時間戳` + `-tw.json`（在 URL 欄位裡直接插入變數）。
   - 方法：**PUT**。
   - 標頭：`Authorization` = `Bearer github_pat_…`（貼上 0.1 的權杖）；`Accept` = `application/vnd.github+json`。
   - 要求本文：**JSON**，三個欄位（都選「文字」）：`message` = `snapshot ` + `時間戳` + ` tw`；`content` = 第 7 步的 Base64 編碼結果；`branch` = `main`。

### 1.3 ESPN（國際盤，DraftKings）
9. **取得 URL 內容**：URL `https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=` + `美東日期`；GET。
10. **Base64 編碼**（換行：無）。
11. **取得 URL 內容**（上傳）：同第 8 步，只把檔名結尾 `-tw.json` 改成 `-espn.json`、message 結尾改成 ` espn`、content 改成第 10 步的結果。

### 1.4 The Odds API（Pinnacle 等；06:00 跳過以省額度）
12. **如果** `小時` **不是** `6`：
    - **取得 URL 內容**：URL `https://api.the-odds-api.com/v4/sports/basketball_nba/odds?apiKey=<你的金鑰>&bookmakers=pinnacle,draftkings,fanduel&markets=spreads,totals&oddsFormat=decimal`；GET。
    - **Base64 編碼**（換行：無）。
    - **取得 URL 內容**（上傳）：同第 8 步，檔名結尾 `-oddsapi.json`、message 結尾 ` oddsapi`。
    - **結束如果**。

完成後先按一次右下的 ▶ 手動執行。到 GitHub 的 `nba-predict-data` → `tw_snapshots/<今天>/` 應該看到 3 個檔案（如果現在剛好是 6 點就是 2 個）。

---

## 2. 設定自動執行

1. 捷徑 App → 下方 **自動化** → 右上 **＋**（或「新增自動化」）→ **特定時間**。
2. 時間 **21:00**、重複 **每天**。
3. 選 **立即執行**（不要選「執行前詢問」）；如果有「執行時通知」可以關掉。
4. 下一步 → 選捷徑 `台彩快照` → 完成。

**先只建這一個，做鎖定測試：**
- 暫時把這個自動化的時間改成「現在 + 2 分鐘」，把手機鎖起來放著。
- 時間過後到 GitHub 看有沒有新的 3 個檔案。
- 有 → 把時間改回 21:00，再建立其他 6 個：**06:00、07:00、08:00、09:00、10:00、11:00**（設定一樣）。
- 沒有 → 先不要建其他的，把情況告訴 Claude（可能是鎖定時某個動作需要解鎖）。

---

## 3. 平常要注意
- 手機在台灣、有網路、有電就會自動跑。出國時台彩會擋，回來再恢復即可（那段期間的資料會缺）。
- 任何一個網址回 **403** 或驗證頁：**把 7 個自動化都關掉**，告訴 Claude。不要改網址或換方法重試。
- 上線前請你自己用瀏覽器看一次：台彩官網的服務條款，以及 NBA 讓分、大小分是否含延長賽。
- 電腦端要 push 資料 repo 之前，先在 `data/` 裡 `git pull --rebase`（手機也在寫 `main`）。

## 4. 怎麼停用
- 暫停：捷徑 App → 自動化 → 把 7 個自動化逐一關閉。
- 永久停用：GitHub → Settings → Developer settings → Fine-grained tokens → 這把權杖 → **Revoke**。撤銷後捷徑就再也寫不進去。
