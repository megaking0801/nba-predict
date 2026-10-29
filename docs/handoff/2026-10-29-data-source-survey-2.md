# 歷史特徵資料來源調查（第二輪，2026-10-29，唯讀）

目的：使用者問「不一定只有 ESPN，開源與其他網站還有什麼數據可以用」。這份只談**回測用的歷史資料**；賽前即時資訊見 `2026-10-28-data-source-survey.md`。
方法：先廣搜，再對有希望的線索深入讀頁（WebSearch 加 WebFetch）。**標「已讀頁面」的才是我實際打開核對過的；標「搜尋摘要」的只看到搜尋結果的描述，未驗證。**

## 先講限制
- `CLAUDE.md`：stats.nba.com 會擋 GitHub runner 與非美國 IP。所以下表裡「在 GitHub／Kaggle 上已預先抓好的歷史檔」比「自己連 stats.nba.com」實際得多，下載一次放進資料 repo 即可（單檔壓縮後超過約 10 MB 先問使用者）。
- 授權各不同；Kaggle 每個資料集要單獨看；爬取站台可能有服務條款限制。下載前要使用者同意（檔名、來源、大小）。
- 資料多不等於有用：P4／C1 已證明新特徵很難贏過盤口。新資料只有在「賽前已知、且 ESPN 沒有」時才有意義。

## 來源表
| 來源 | 內容 | 涵蓋 | 授權／取得 | 狀態 |
| :- | :- | :- | :- | :- |
| [shufinskiy/nba_data](https://github.com/shufinskiy/nba_data) | stats.nba.com 逐筆 play-by-play、shotdetail（含座標）；pbpstats 回合與球權；matchups（誰防誰） | PBP 與 shotdetail 1996/97 起；pbpstats 2000/01 起；data.nba.com 2016/17 起；matchups 2017/18–2024/25；到 2024/25 | Apache-2.0；`.tar.xz` 內含 CSV；可 git clone、R／Python 載入器、Kaggle（1996–2021 版）、Google Drive 全檔 | 已讀頁面。最後更新 2025-02-26，2025-26 之後的資料沒有 |
| Kaggle「NBA Stats Dataset 1996–Present」（chevronronson） | 含季後賽，到 2025-26 | 1996-97–2025-26 | 搜尋摘要稱 CC0 | 搜尋摘要，未開頁，授權待核 |
| Kaggle 逐場 play-by-play（Szymon Jóźwiak） | NBA.com 逐筆資料 | 1996-97 起 | CC BY-NC-SA 4.0，禁商用 | 搜尋摘要 |
| Kaggle「PBP with On-Court Lineups 1999-2022」（xocelyk） | 逐筆加場上五人 | 1999–2022 | 未確認 | 搜尋摘要 |
| [wyattowalsh/nbadb](https://github.com/wyattowalsh/nbadb) | stats.nba.com 抽取與整理管線（Kaggle basketball 資料庫） | 未確認 | 未確認 | 搜尋摘要 |
| [hoopR](https://hoopr.sportsdataverse.org/)／sportsdataverse | 批次 PBP 釋出檔（2002 起）、賽程、box score、名單 | 2002 起 | 開源（R／Python） | 來自 awesome-nba-data 列表（已讀），未逐項驗證 |
| [pbpstats](https://github.com/dblackrun/pbpstats) | 由 PBP 推出回合、五人組合、投籃區域脈絡 | — | 開源解析器；網站 API 要付費 | 已讀列表 |
| [nba_api](https://github.com/swar/nba_api) | stats.nba.com 非官方客戶端：追蹤、Hustle、投籃、關鍵時刻、陣容端點 | — | MIT；需瀏覽器等級標頭；端點可能無預警變動 | 搜尋摘要；沒找到 Synergy 端點 |
| 82Games、Cleaning the Glass、NBAstuffer | 陣容、on/off、垃圾時間過濾 | — | CTG 付費 | 已讀列表 |
| EPM（Dunks & Threes）、LEBRON、DARKO、RAPM、RAPTOR 歷史 CSV（FiveThirtyEight，已封存） | 球員影響力指標 | RAPTOR 到封存日 | 各自授權 | 已讀列表；**這些是賽後估計，用於賽前特徵要小心洩漏（只能用「該場前」的版本）** |
| [Sportsbook Review 賽季 Excel](https://github.com/isaacrome21-sys/SportsEdge/issues/2605)（經 GitHub issue 提到） | NBA 開盤與收盤的讓分、總分 | 2007 起（據 issue） | 原網址已下架，Internet Archive 有抓取（據該 issue） | 搜尋摘要，我沒找到存檔本身 |
| [Kaggle NBA 2025-26 Closing Lines](https://www.kaggle.com/datasets/minmaxer/nba-closing-lines-ats-2025-26) | 開盤與收盤的讓分、總分、不讓分 | 僅 2025-26，免費樣本 | 完整版付費 | 搜尋摘要 |
| 其他 Kaggle 賠率集（ehallmar、cviaxmiwnptr、christophertreasure） | 讓分、總分、不讓分，名稱稱 2007–2024 | 未確認是否有開盤 | 未確認 | 搜尋摘要，覆蓋與品質待核 |
| Princeton DSS Historical Sports Odds | NBA 開盤與收盤讓分、總分 | 2006–2023 | 可能限校內帳號（未驗證） | 搜尋摘要 |
| The Odds API（歷史） | 賠率快照 | 2020 年中起，付費方案 | 付費 | 搜尋摘要 |
| 官方 NBA 傷兵報告；PyPI `nba-injury-report` | 賽前參賽狀態 | 2021-22 起（套件自述） | 公開 API；套件版本 0.1.0，使用前要讀程式碼 | 搜尋摘要 |
| [Statsurge 傷兵資料](https://statsurge.substack.com/p/downloadable-nba-injury-datasets) | 歷史 CSV 與每日更新資料庫 | 未確認起點 | 未確認 | 搜尋摘要 |
| 裁判：Basketball-Reference 裁判頁、NBA Last Two Minute 報告封存、The Pudding 資料集 | 裁判生涯統計；關鍵兩分鐘報告 | L2M 2015-03 至 2025-06 | 官方 L2M 公開網頁；沒有批次下載 | 搜尋摘要。**我們 DB 的 `game_officials` 已有每場裁判名單（P2），裁判資料不需外部來源** |

## 我的判斷（含不確定處）
1. **最值得下載的是 shufinskiy/nba_data**：Apache-2.0，已預先抓好、不必連 stats.nba.com，且有兩樣 ESPN 沒有的東西：逐筆 shotdetail 座標與「matchups（誰防誰）」。但覆蓋到 2024/25，且先驗上我不看好（見下）。
2. **賠率歷史**是回測的命脈，但我們已有 ESPN 的開盤／收盤近似（`market_lines`）。外部賠率集只能用來交叉驗證與補 2007–2014 年，用處有限；而且 P5 顯示模型贏不了盤口，更多年份不會改變結論。
3. **追蹤資料（SportVU）**公開鏡像有「研究用、不得公開再散布」的聲明，我不建議碰。
4. 賽後型指標（EPM、RAPTOR、LEBRON）若要用，必須是賽前版本；多數公開檔是賽季末重算，會洩漏。

## 先驗
- 資料量不是瓶頸（學習曲線約三季就攤平），市場已吸收公開資訊（P5.0）。外部新來源讓模型贏過盤口的先驗我估 <5%（我的估計，不是量測）。
- 真正的價值在於：把「全部特徵族群」一次盤點與防多重檢定地回測，得到「確實找不到」的完整結論；設計由支線寫在 `2026-10-29-stat-universe-design.md`。

## 第三輪核對結果（2026-10-30，已讀頁面；仍有未驗證處）
- **Sportsbook Review 賽季 Excel**（[GitHub issue 的說明](https://github.com/isaacrome21-sys/SportsEdge/issues/2605)）：該 issue 說每季一個 Excel，2007-08 到 2022-23，含收盤讓分、總分、不讓分，且開盤與收盤共用欄位（可算開盤到收盤的移動）。原網址已 404，只剩 Internet Archive 的抓取；**2022-23 那份只到 2023-01-16（664 場，賽季中途抓的）**。檔案沒有授權聲明，受 SBR 網站條款限制。2023-24 與 2024-25 沒有免費的完整檔。我沒有自己下載或核對檔案內容，以上是該 issue 作者的描述（**未驗證**）。價值：只對 C2「開盤到收盤」的歷史補充有一點用，且缺最近兩季。
- **PyPI `nba-injury-report` 0.1.0**：抓官方 NBA 傷兵報告，2021-22 起；**授權 AGPL-3.0**（強著作權，放進我們的公開 repo 或服務要小心，建議只當參考、自己寫抓取）；需 Python 3.12；只有一個版本（2026-01-07）、一位維護者；頁面沒寫實際端點，也沒寫條款。結論：不使用。
- **Statsurge 傷兵 CSV**：2021 年 10 月到 2024 年 6 月，三個賽季的 CSV，連結是 bit.ly；**無授權聲明、資料來源未說明、已不再維護**。結論：不使用；我們自己的 `injury_reports` 從 2026 年開始累積。
- **nba_api 端點**（只看到 GitHub 目錄的前半段，後半被截斷）：有 `boxscore*v3`（進階、四因子、hustle、matchups、球員追蹤）、`playbyplayv3`、`leaguedashlineups`、`leaguedash*ptshot`、`leaguedashptdefend`、`leaguehustlestatsplayer／team`、`leaguedashplayerclutch`、`playerestimatedmetrics`、`gamerotation`、`leagueseasonmatchups`。這些都來自 stats.nba.com，而 `CLAUDE.md` 規定 stats.nba.com 會擋 GitHub runner 與非美國 IP，所以只能在本機（美國以外 IP 也可能被擋）或用已預先抓好的檔。是否使用待使用者決定，我不建議現在做。
- **Kaggle「NBA Stats Dataset 1996–Present」**：我猜的網址回 404，授權與欄位**未驗證**，不要引用「CC0」。

## 仍未做
- 找到 chevronronson 資料集真正的網址再核對授權。
- 實際取得 SBR 的 Internet Archive 檔案並核對欄位（需要使用者同意下載：檔名、來源、大小要先報）。
