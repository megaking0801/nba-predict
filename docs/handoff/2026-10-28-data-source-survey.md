# 賽前資料來源調查（2026-10-28，唯讀）

目的：為「價格落後」實驗（`2026-10-28-prematch-info-evaluation.md`）找 ESPN 以外的來源。範圍：傷兵消息、先發名單、盤口（即時／歷史）、球員休息／出賽狀態。

## 方法與限制（請先讀）
- 只用 WebSearch 與 WebFetch 讀**說明、定價、條款頁**。**沒有**對任何來源發出資料請求，也沒有測試過連線，所以「GitHub runner 是否被擋」全部是**未測**；只有 CLAUDE.md 已記載的「stats.nba.com 會擋 GitHub runner 與非美國 IP」是既知事實。
- WebFetch 會先經一個小模型摘要，下面的引文是摘要模型回傳的，不是我逐字讀到的全文。要依條款做決定前，請使用者自己讀原文。
- 條款欄位：**已讀**＝我讀到了條款頁（經摘要）；**部分**＝讀到一部分；**未讀**＝沒讀到（頁面 404、被截斷或沒找到）。價格與限額來自廠商頁面，會變動。

## 先講一個與本專案現況有關的發現
Disney 使用條款（涵蓋 ESPN）寫著不得以「robot, spider, script, or other automated means」存取或複製其產品，也不得「compiling, building, creating or contributing to any collection of data」，且授權僅限「personal, noncommercial use only」。本專案現在用腳本定期讀 ESPN 的非公開 JSON endpoint（`site.api.espn.com`、`sports.core.api.espn.com`）並存進 DB，**依這份條款字面看並不被允許**（我讀的是通用的 Disney 條款；ESPN 是否有另外的補充條款，**未讀**）。這不是新問題，但使用者要求「條款允許」，所以我把它列出來，由主線與使用者決定怎麼處理。

## 來源表
| 來源 | 資料內容 | 更新頻率／延遲 | 取得方式 | 費用 | 條款允許自動讀取？ | GH runner 是否被擋 | 對價格落後實驗 |
| :- | :- | :- | :- | :- | :- | :- | :- |
| **The Odds API**（the-odds-api.com） | 多家莊家的讓分／大小分／不讓分（`spreads` 主要涵蓋美國盤口與美國莊家）；每個莊家與每個市場有 `last_update` | 頁面未寫刷新頻率；市場關閉後約 15 分鐘從回應移除；比分約 30 秒更新 | REST API，要免費金鑰。`/v4/sports/{sport}/odds?markets=spreads&regions=us`，**一次呼叫回傳全部比賽**，費用 ＝ 市場數 × 區域數（1×1＝1 點） | 免費方案 500 點／月（第三方頁面，官方定價頁我沒讀到）；歷史端點「只限付費」，費用 10 × 市場 × 區域；付費約 $30／月 20,000 點（第三方，未驗證） | **已讀**：允許。「Storing our data and retaining it indefinitely」列為允許用途；允許商業使用，但不得「resell, repackage, or redistribute our data as a standalone data product」；懷疑濫用可終止 | 未測；它是為程式存取設計的公開 API，一般不擋雲端 IP | **高**：每家莊家的 `last_update` 可以直接看出誰更新慢；沒有歷史，只能從現在起自己累積 |
| **NBA 官方傷兵報告**（official.nba.com，PDF） | 官方傷兵、疾病、**休息**及參與狀態 | 頁面寫「Reports are updated on a continual basis throughout the day」，列出每 15 分鐘一份；隊伍須在前一天當地時間 17:00 前、背靠背第二場為當天 13:00 前提交；比賽日報告 11:00～13:00（下午 5 點前的比賽為 08:00～10:00） | 靜態 PDF，檔名樣式 `https://ak-static.cms.nba.com/referee/injury/Injury-Report_YYYY-MM-DD_HH_MMAM.pdf`（由頁面範例推得）；需要解析 PDF（社群套件 `nbainjuries` 用 Java＋tabula，MIT 授權，稱 2021-22 賽季起有歷史、季前賽與季後賽有缺口） | 免費 | **部分**：頁面寫「No portion of NBA.com may be duplicated, redistributed or manipulated in any form」。NBA.com 使用條款（已讀）只准個人非商業用途，並寫「NBA Statistics may not be used in connection with any gambling activity」；injury report 算不算 NBA Statistics 我無法判斷。條款沒有點名 robots／spiders | 未測。同一家的 stats.nba.com 已知會擋；`ak-static.cms.nba.com` 沒有證據，**必須先測** | **最高（時間戳）**：這是最早的官方消息，15 分鐘一份的檔名本身就是時間戳 |
| **NBA CDN 賠率 JSON**（`cdn.nba.com/static/json/liveData/odds/odds_todaysGames.json`，見 `nba_api` 文件） | 當天比賽的多家莊家（含莊家名稱、國家碼）、**開盤與現盤**、趨勢、讓分 | 文件沒寫；文件最後驗證日 2025-03-10 | 靜態 JSON，免金鑰 | 免費 | 同 NBA.com 使用條款（見上）；`nba_api` README 也提醒有 Terms of Use | 未測。官方端點已知會擋 GitHub runner 與非美國 IP，這個網址**必須先測** | **高**：免費、多莊家、有開盤與現盤；但「賭博相關」條款風險同上，且 endpoint 是否仍存在未驗證 |
| **SportsGameOdds**（免費方案） | NBA 的盤口，9 家莊家（含 FanDuel、DraftKings、BetMGM） | 免費方案 10 分鐘更新；付費 Rookie $99／月 3 分鐘、Pro $299／月不到 1 分鐘 | REST API，要金鑰 | 免費：每分鐘 10 次、每月 2,500 個 objects；無歷史（歷史在 Pro） | **未讀**（只看了定價頁） | 未測 | 中：10 分鐘更新對 60 分鐘的窗口剛好夠，但 2,500 objects 限額偏緊 |
| **OddsPapi** | 多家莊家（頁面列出 Pinnacle、Bet365、DraftKings 等），自稱免費歷史資料 | 自稱 WebSocket 低延遲；數字在另一頁，**沒讀到** | REST／WebSocket，要金鑰 | 免費額度**沒讀到**（第三方稱 500 requests／月） | **部分**：條款提到「monitor usage for abuse such as exceeding limits, sharing API keys, or scraping」，不得轉售為獨立資料產品；儲存與快取、免費方案限制**未寫** | 未測 | 中：若免費歷史屬實對回測很有用，但這是廠商自述，未驗證 |
| **balldontlie**（`nba.balldontlie.io`） | 傷兵 `GET /v1/player_injuries`；賠率；先發名單 `/v1/lineups`；球員 props | 傷兵、賠率沒寫頻率；lineups 官方註明「only available once the game begins」 | REST API，要金鑰 | Free $0（5 次／分鐘，只有基本資料）；ALL-STAR $9.99／月（60 次／分鐘，**含傷兵**）；GOAT $39.99／月（600 次／分鐘，**含賠率、lineups、props**） | **未讀**（條款連結沒進摘要） | 未測 | 傷兵：中（$9.99，延遲未知）；賠率：要 $39.99；lineups 賽前沒有，**無用** |
| **ESPN 隱藏 JSON**（目前在用） | 傷兵（含 `reported_at`）、名單、盤口（單一供應商） | 傷兵約每天抓 4 次；盤口刷新頻率未量 | 非公開 endpoint | 免費 | **已讀 Disney 通用條款：字面上不允許**（見上）；ESPN 專屬條款未讀 | 現有排程在 runner 上可用（運作中） | 已在用；代理品質待階段 0 量測 |
| **Jbot（台彩）** | 台彩賽前盤 | 一次性查詢 | API，要金鑰 | 試用金鑰 | 依 `jobs/jbot.py` 所記：禁止自動重複、每天 20 次共用、僅研究用（主線 2026-10-26 讀過，**我未重讀**） | 未測 | 不能做定時快照，只能手動比對 |
| **Rotowire／CBS／Yahoo 等媒體** | 傷兵與先發快訊 | 通常最快（記者消息） | 網頁／RSS | 免費或訂閱 | **未讀**：Rotowire 條款頁 404；CBS、Yahoo 沒找到。NBA.com RSS 條款寫明僅供個人非商業 | 未測 | 時間戳最有價值，但沒有條款允許的自動取得方式，**不建議** |
| **Basketball-Reference／Sports Reference** | 球員新聞頁、歷史數據 | — | 網頁 | 免費 | **未讀** | 未測 | 低 |

## 補充：使用者的網路位置
`jobs/espn_http.py` 開頭記載 stats.nba.com「Akamai geo/IP-blocks both GitHub runners and non-US homes」。使用者在台灣，**從他自己家裡連 NBA 官方端點也可能被擋**（`ak-static.cms.nba.com` 的 `ak` 可能表示 Akamai，是推測）。所以第 2、3 名的「請使用者手動開一份 PDF 確認可達」在列入計畫前就要先做；不可達的話它們都不用考慮。

## 先發名單與休息
- **先發名單**：沒找到「賽前、免費、條款明確允許自動讀取」的來源。balldontlie 的 lineups 只在比賽開始後才有（官方註明），對賽前無用；媒體的先發預測頁條款未讀。官方傷兵報告不含先發。→ **無法評估**。
- **球員休息／出賽狀態**：官方傷兵報告的規定範圍明寫包含「injuries, illnesses, other medical conditions, or rest」，所以休息與傷病在同一份文件；除此之外沒有獨立來源。

## 盤口歷史（回測用）
- 免費且涵蓋 NBA 收盤讓分的資料很少。Princeton 的 Historical Sports Odds Database 目錄列出 NBA 2006～2023 的開盤與收盤讓分、大小分（可能需要校內身分，**未驗證**）；另有文獻指向 sportsbookreviewsonline.com 的歷史賠率檔（目前內容與涵蓋年份**未驗證**）。Kaggle 上找到的 NBA 資料集多是比賽結果與 Elo，沒有盤口。
- 本專案已有自己的 14,732 列歷史盤口（全是 ESPN 來源），夠做目前的回測；價格落後實驗**需要的是「時間密度」，不是歷史長度**，免費歷史對它幫助有限。

## 過濾後的建議前三名
排序依據：條款是否明確允許、費用、延遲、對實驗的貢獻。

1. **The Odds API 免費方案（盤口）。** 唯一一個「條款讀過、明確允許程式存取與長期儲存」的盤口來源；一次呼叫（1 點）回傳全部比賽的多家莊家，每家都有 `last_update`，正好能直接量出「哪一家更新慢」。限制：500 點／月≈每天 16 次呼叫，只夠事件觸發式取樣，不夠 5 分鐘輪詢；沒有歷史；Pinnacle 是否在清單內我**未確認**（沒有 Pinnacle 的話，「快的參考線」要改用多家共識）。需要使用者註冊並保管金鑰（免費，不涉及付款）。
2. **NBA 官方傷兵報告 PDF（消息時間戳）。** 最權威、最早的官方來源，每 15 分鐘一份，檔名即時間戳，免費。**但條款有風險**：NBA.com 條款限個人非商業用途，且寫了「NBA Statistics may not be used in connection with any gambling activity」；這個實驗目的就是觀察賭盤的反應，**我不能替使用者判斷這算不算違反**，請使用者自己讀 https://www.nba.com/termsofuse 再決定。另外 `ak-static.cms.nba.com` 對 GitHub runner 是否可達**未測**。建議：只在使用者自己的電腦上低頻讀取，並且先由使用者手動開一份 PDF 確認可達。
3. **NBA CDN 賠率 JSON（多莊家現盤與開盤）。** 免費、多家莊家、有開盤與趨勢，若仍可用，單一請求就是一份多來源的盤口快照，能補第 1 名的點數限制。風險與第 2 名相同（NBA 條款、可達性未測、endpoint 是否還在未驗證）。
   - **如果使用者認定 NBA 條款擋住第 2、3 名**：傷兵改用 balldontlie ALL-STAR（$9.99／月，**要花錢，須使用者同意**，條款與延遲都未讀未知）；第二個盤口意見用 SportsGameOdds 免費方案（10 分鐘更新，條款未讀）。

## 不建議
- 媒體網站（Rotowire 等）：沒有條款明確允許的自動取得方式。
- balldontlie 的 lineups：賽前沒有資料。
- 任何需要繞過 Cloudflare、登入或其他防護的來源（含台彩官網）。

## 建議的下一步（由使用者決定，我沒做）
1. 使用者自己讀 NBA 使用條款，決定第 2、3 名可不可用。
2. 若要用 The Odds API：使用者註冊免費金鑰（我不經手金鑰）；先用幾個點數確認 `us` 區域有哪些莊家、`last_update` 的實際粒度，再訂事件觸發的取樣表。
3. 在任何自動化之前，先由使用者手動確認 `ak-static.cms.nba.com` 與 `cdn.nba.com` 從他的網路是否可達，GH runner 是否可達用單次手動 workflow 測（需使用者同意觸發 workflow）。
4. 處理 ESPN 現有用法與 Disney 條款的差距（主線與使用者決定）。

## 參考（本次讀過的頁面）
- NBA 傷兵報告頁：https://official.nba.com/nba-injury-report-2025-26-season/
- NBA.com 使用條款：https://www.nba.com/termsofuse
- Disney 使用條款：https://disneytermsofuse.com/english/
- The Odds API 文件：https://the-odds-api.com/liveapi/guides/v4/ ；條款：https://the-odds-api.com/terms-and-conditions.html
- balldontlie：https://www.balldontlie.io ；https://nba.balldontlie.io
- SportsGameOdds 定價：https://sportsgameodds.com/pricing
- OddsPapi：https://oddspapi.io/en ；條款：https://oddspapi.io/en/legal/terms
- `nba_api` 賠率文件：https://github.com/swar/nba_api/blob/master/docs/nba_api/live/endpoints/odds.md
- `nbainjuries`：https://pypi.org/project/nbainjuries/
- 第三方比較（僅供線索，未當作事實）：https://oddspapi.io/blog/the-odds-api-free-tier-limits ；https://sportsapis.dev/free-odds-api
