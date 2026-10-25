# P3 資料品質檢查：設計

日期：2026-10-25
狀態：草稿，待 NBA-主線 對照原始資料審查，再轉使用者（使用者已授權兩個 session 決定細節）
上層：`docs/superpowers/specs/2026-10-23-roadmap-design.md` §6 P3；前置：P2 已回補 2015-16 到 2026-27（`docs/handoff/2026-10-23-roadmap.md`「P2 回補進度」）
原始數字：`docs/handoff/p3-sql-results-2026-10-25.md`（主線用唯讀 MCP 跑的全量 SQL，下稱「SQL 結果」）

**標示約定**：每個事實後面標來源。
- **[全量]**：對整個 DB 跑 SQL 的結果（SQL 結果檔，括號內為查詢代號）。
- **[抽樣 n=…]**：我（支線）打 ESPN 公開 API 抽樣，n 是樣本數，只代表「有這種現象」，不代表比例。
- **[推測]**：由證據推論、尚未直接驗證。
- **[程式]**：讀原始碼得到（附檔案與行號）。

## 1. 目的

P4（特徵）與 P5（模型）要用 12 季資料。P3 要回答三件事，並讓答案可以自動重跑：

1. 哪些資料可信，哪些有已知缺陷。
2. 有缺陷的資料，P4／P5 該排除、當缺值，還是加權。
3. 每次回補或每日抓取之後，怎麼自動發現「新的」問題，而不是每次都被已知問題淹沒。

## 2. 目標、非目標、反目標

**目標**
- 一份可執行的檢查清單（id、嚴重度、判斷方式、門檻、已知基準）。
- 一個只讀的每場品質旗標檢視表 `v_game_quality`，P4／P5 用它篩選，不必各自重寫判斷。
- 一個只讀的檢查入口 `python -m jobs.quality`，可在回補後與每日排程後執行。
- 對 P5 的明確建議（盤口、疫情季、出手桶、佔位資料）。

**非目標**
- 修正或刪除現有資料。**P3 設計與其檢查程式一律只讀**；任何修補（重抓、補欄位）列為獨立的後續項目（§9），各自走計畫與使用者同意。
- 特徵、模型、gates（P4、P5）。
- 變更現有表的結構或唯一鍵。

**反目標**
- 為了讓檢查全綠而調鬆門檻或刪掉「不方便」的檢查。已知問題用「基準線」記錄（§7.3），不是用放寬門檻隱藏。
- 把 ESPN 的欄位當作真值而不核對（§3 有多個例子：球場名稱、開賽時間、`pointsAttempted` 都出過錯）。
- 用規則去「修」出手桶使其對上 box score（§4.4 說明為什麼）。

## 3. 已驗證的現況

### 3.1 場次完整性
- ESPN scoreboard 全季爬取（12 季，0 個失敗日期；對 ESPN 的全量唯讀爬取，原始結果存在 `docs/handoff/p3-espn-scoreboard-audit-2026-10-25.json`）與 DB 全量 SQL 對照 [全量]：ESPN 上「NBA 對 NBA、已完賽」的 preseason／regular／playoffs 場數，與 DB（SQL 結果 S2a）**每季完全相同**，例如 2015-16 為 100／1230／86、2020-21 為 49／1080／85、2023-24 為 64／1231／82、2025-26 為 65／1231／85。P2 沒有漏收任何「可收」的比賽。
- 被略過的 ESPN 事件共 91 筆（66＋19＋6，已用程式逐類重數）[全量爬取]：
  - 全明星賽 19 場完賽（ESPN 標成 `regular`，隊名為 EAST／WEST、USA／WORLD、Team LeBron／Giannis／Durant、CHK／SHQ／KEN／CAN、STARS／STRIPES／WORLD 等）；
  - 季前賽的國際或海外球隊 66 場（MCH、GUA、NZL、MEL、BAURU、FLMG、LON…）；
  - 其餘為 2026-27 的 6 場 `TBD at TBD`（regular、scheduled，2026-12-04～12-11，NBA Cup 淘汰賽前的對手未定佔位）。
  - 沒有任何一筆是「應該收卻沒收」的 NBA 對 NBA 比賽。
- 延期（postponed）的 regular 事件各季都有（2020-21 有 32、2021-22 有 11）[爬取結果]；DB 的 regular 場數與 ESPN 的 final 場數一致 [全量 + 爬取]，所以沒有重複或遺漏。
- 例行賽場數：2015-16～2018-19、2021-22、2022-23 每季 1230；2019-20 為 1059；2020-21 為 1080 [全量，S2a]。每隊場數：2015-16～2018-19、2021-22、2022-23 全是 82；2020-21 全是 72 [全量，S2b]。2019-20 隊場數 64～75 不等，原因（停賽與 8 場補賽）是記憶中的說法，**未驗證**。
- **2023-24、2024-25、2025-26 的 regular 是 1231 場，28 隊 82 場、2 隊 83 場** [全量，S2a／S2b]。多出來的那場是 NBA Cup 冠軍賽，ESPN 標成 `regular`：2023-12-09 LAL 123–109 IND（401607495）、2024-12-17 OKC 81–97 MIL（401734908）、2025-12-16 NYK 124–113 SAS（401809839）[全量，S2c]。2026-27 的冠軍賽尚未打。
- 沒有「同日同對戰重複」、沒有 final 但缺比分／分差、沒有平手、沒有超過 3 天仍非 final 的場次、球隊 `pts` 與比賽比分一致 [全量，S2f 的五項皆為 0]。
- `team_twice_same_day` 有 1 筆：2020-03-01 的 GSW [全量，S2f]。ESPN 自己的資料：401161536（GSW @ PHX）`date`＝2020-03-01T12:30Z（ET 清晨 7:30），venue 寫成 "Paycom Center"（OKC 的球館）；401161543（WSH @ GSW）`date`＝2020-03-02T01:30Z [抽樣 n=2，ESPN API]。結論：ESPN 的開賽時間與場館欄位**會錯**。ET 開賽小時 < 10 的完賽場共 26 場（22 preseason、4 regular 都在 2019-20 的 2020-02-24～03-01）[全量，S6／S6b]；`tipoff_utc` 為空 0、ET 日期與 `game_date_et` 不符 0。

### 3.2 box score
- 所有完賽場次都有 2 列球隊與至少 10 列球員（`few_player_rows`、`not_2_team_rows` 皆為 0）[全量，S2d]。**但這不代表資料有內容**，見下。
- **佔位 box（兩隊同時）**：2015-16 regular 161 場、2016-17 regular 163 場＋playoffs 5 場、2017-18 regular 163 場＋playoffs 9 場、2020-21 play-in 6 場（全部）、2021-22 preseason 7 場 [全量，S5／S5b]。這些場次：
  - 球員列都在，但 `minutes` 為 `--`（存成 NULL）、`points` 與所有數字為 0（實測 400899826 MIL–CHI：18 列球員全部分鐘為 NULL、得分 0，球隊得分 108／97）[抽樣 n=2，ESPN API]；DB 的 `min_null` 共 11,210 列 [全量，S2h]，其中應有大部分來自佔位 box（佔位隊場 322＋336＋344＋12＋14＝1,028 個；實測佔位場 400899826 兩隊共 18 列球員、即每隊約 9 列，1,028×9≈9,250，約占 83%），其餘約 2,000 列來源未拆分 [推測]。
  - **球隊層統計也是 NULL**：`team_game_stats` 的 `fga／fg3a／fta／tov／oreb／dreb` 在佔位隊場次與佔位球員 box 完全重合（隊場數相同）[全量，S5]。（我先前說「球隊層 box 正常」是錯的，已更正。）
  - 影響 [程式] `jobs/features.py:265-270`：`_team_game_metrics` 在 `fga` 為 NULL 時把欄位當 0，`own_poss <= 0` 就回傳 `None`；`update` 因此**靜默略過**該場的四要素與節奏更新，但場次與比賽日仍被計入（`gp`、`dates`）。`jobs/features.py:320` 把 `min_played` 非正的球員濾掉，球員層更新也整場略過。不會報錯，只是那場沒有貢獻資訊。
- 2024-25 另有 10 個隊場次 `team_game_stats` 的統計欄位 NULL，但球員 box 正常 [全量，S5]。
- 球員得分加總 ≠ 球隊得分、且球員 box 有實際分鐘：2018-19 有 23 隊場、2025-26 有 14 隊場、2019-20 有 2、2020-21 有 1、2015-16 有 1 [全量，S5 的 `real_players_but_pts_off`]。原因未查。
- 分鐘合計應為 240＋25×延長賽節數；不符（容許 ±3）的隊場次：2018-19 有 21、2025-26 有 23、2023-24 有 5、2021-22 有 4、2020-21 有 3，其餘季 0～2；最低 180（2015-16）、最高 341（2018-19）[全量，S2e]。原因未查。
- 球員列異常：`min_played` 為 NULL 11,210 列（主要是佔位）、為 0 有 1,182 列、大於 60 有 1 列、不可能的數字組合（`fgm > fga` 等）0 列 [全量，S2h]。
- 球員得分加總與球隊得分：2018-19 起整體很乾淨，2022-23～2024-25 全部吻合 [全量，S2e]。

### 3.3 出手距離桶（`team_shot_buckets`）與 box score 的 FGA
- **兩種不同的不吻合**：
  1. **舊季 plays 比 box score 少**。吻合率要用「可比對的隊場」當分母（排除 box 的 `fga` 為 NULL 的佔位隊場，因為它們的差值是 NULL，不屬於任何一類）[全量，S1，我用 S1 各類加總重算：可比對數＝team_games−佔位隊場]：
     - 2015-16 regular：可比對 2138（2460−322），吻合 1115＝**52%**、少 1～2 筆 477（22%）、少 >2 筆 538（25%）、多 1～2 筆 5、多 >2 筆 3；
     - 2016-17 regular：可比對 2134（2460−326），吻合 1987＝**93%**、少 1～2 筆 92、多 1～2 筆 55，沒有 >2 筆；
     - 2017-18 regular：可比對 2134，吻合 2020＝**95%**、少 1～2 筆 73、多 1～2 筆 37、>2 筆共 4；
     - 2018-19 regular：可比對 2460，吻合 2361＝**96%**。
     所以「舊季 plays 不完整」主要是 **2015-16** 的現象；2016-17、2017-18 與 2018-19 以後同一量級（我先前用 2460 當分母寫成 81%／82%，是錯的，已更正）。原因是逐回合紀錄不完整 [推測]。
  2. **2025-26 起 plays 比 box score 多 1～2 筆**：2025-26 regular 2462 隊場，吻合 1517（61.6%）、多 1～2 筆 914、多 >2 筆 21；2026-27 regular 吻合 60%（43／72）[全量，S1]。每個月都偏（吻合率 57%～70%），**不是某月起**；2024-25 每月 ≥ 94% [全量，S1b，主線更正後的數字]。
- 2025-26 多出來的原因 [抽樣 n=22 個不吻合隊場 + 5 場細查，ESPN API；推測]：多出的全是**未進球**出手（我細查的 3 場進球數與 box score 完全相同），且多半是節末（時鐘約 0.0）的長射，下一筆是球隊籃板，例如 401810084 OKC 第 3 節末 Gilgeous-Alexander 的 heave、401809979 CHA 第 2 節末 Mann 的 heave。推測 ESPN 從 2025-26 起在逐回合紀錄收錄了官方 box score 不計的節末 heave。我試過規則「時鐘 ≤ 3 秒、未進球、下一筆是球隊籃板」，只能解釋約一半不吻合的隊場，且同類出手在許多吻合的場次也存在 [抽樣 n=38 場（76 隊場）]。**所以規則修不了，不應該用規則修。**
- 3 分出手數同步不吻合（2025-26 regular 3PA 不符 936 隊場）[全量，S1]。
- 2025-26 有 8 場季後賽（04-25～05-23）**沒有出手桶也沒有 `player_game_status`**，但 `player_game_stats` 有 18～26 列 [全量，S7]：401869391 ATL–NYK、401869377 TOR–CLE、401869373 PHX–OKC、401869402 MIN–DEN、401871334 DET–CLE、401871154 MIN–SAS、401871337 DET–CLE、401873343 CLE–NYK。原因未查（可能是這 8 場的 `parse_summary` 當時拋例外而被整場略過，只印 WARN）[推測]。
- 2020-21 play-in 12 隊場「吻合 0」：根因是 `team_game_stats.fga` 為 NULL（佔位），出手桶本身正常（例如 401326988 CHA 94、IND 105）[全量 + 主線查證]。

### 3.4 先發、裁判
- 先發 ≠ 10 人的完賽場：2018-19 有 19 場、2025-26 有 8 場（即上一節沒有 status 列的 8 場季後賽）、其餘各季 0～1 [全量，S2g]。
- 裁判少於 3 位：2020-21 有 40 場、2023-24 有 19、2022-23 有 17、2025-26 有 15、2021-22 有 14、2018-19 有 4 [全量，S2g]。

### 3.5 盤口
- 時間點：2015-16～2022-23 全部是 `current`／`current`（少數場次沒有總分）；2026-27 為 `close` 91＋`current` 11；**2023-24、2024-25、2025-26 的 `price_point` 全是 NULL**（1383／1387／1387 列），且全部 `captured_at = tipoff_utc`，即 P2 之前的回填列 [全量，S3a／S9]。
- ESPN core API 各年代供應的時間點 [抽樣 n=62 場，9 個年代，ESPN API]：2015-16～2022-23 只有 `current`；2023-24 起 `open`、`current`、`close` 齊備，且在我抽到的 31 場裡 `current` 與 `close` 的讓分**完全相同**（差 0.0）。因為舊程式也是「close → current → open」，所以那三季的 NULL 列**很可能就是 close**——這是推論 [推測]，不是全量驗證。
- 覆蓋：regular 沒有盤口的只有 2018-19 的 4 場與 2021-22 的 7 場；playoffs／playin 全有；preseason 幾乎都沒有（2015-16～2019-20 完全沒有）[全量，S3b]。
- 「像不像收盤線」體檢（regular，`v_closing_lines` 對賽果）[全量，S3c]：各季讓分與賽果的相關 0.409～0.532、MAE 9.06～10.83、標準差 11.59～13.90。收盤線的合理範圍約為相關 0.4～0.5、MAE 9～11；若抓到的是賽後或進行中的線，相關會逼近 1、MAE 逼近 0。所以**讓分線各季都像賽前線，沒有看到賽後污染**。
- **總分線有壞資料**（regular）[全量，S8／S8b]：
  - 2015-16 有 1 筆總分線 2001（PHX–SAS 2016-01-21，實際應是 200.1）；2017-18 有 1 筆 −1；
  - **2021-22 有 24 場總分線只有 49.5～60**（例如 MIA–CHA 實際總分 259、線 58），看起來是半場或單隊線，日期散在 2021-10～2022-04；
  - 對應的總分相關：2015-16 為 0.068、2021-22 為 0.146（其餘季 0.355～0.487），2021-22 平均誤差 +3.62（其餘 −1.13～1.23）。這兩季的整體相關偏低，推測就是上述壞資料造成 [推測，尚未驗證排除後是否恢復]。
  - 讓分 |spread| > 25：全部季別 0。
  - 2024-25 與 2025-26 的讓分線 push（賽果正好等於讓分）為 0 [全量，S3c]：可能是線都取 .5，原因未查。

### 3.6 中立場地、疫情
- `neutral_site` 為 TRUE 的 regular 場數：2015-16 2、2016-17 3、2017-18 3、2018-19 3、2019-20 3、2020-21 0、2021-22 0、2022-23 1、2023-24 3、2024-25 6、2025-26 6，全部季別 `neutral_site` 為 NULL 的場數 0 [全量，S4a]。**2020 泡泡賽沒有被標成中立場地**（P2 時已實測 ESPN 回 False）。
- 主場優勢（regular 平均主場分差／主場勝率）[全量，S4a]：2015-16 2.67／0.589、2016-17 3.15／0.584、2017-18 2.11／0.579、2018-19 2.72／0.593、2019-20 2.13／0.551、**2020-21 0.94／0.544**、**2021-22 1.72／0.544**、2022-23 2.50／0.580、2023-24 2.16／0.543、2024-25 1.62／0.543、2025-26 1.74／0.555。
- 疫情區間（含季後賽與 play-in）[全量，S4b]：2019-20 停賽前 n=971，分差 2.17、勝率 0.551；**泡泡 n=172，1.35、0.523**；2020-21 12～2 月 n=501，0.97、0.541；2020-21 3～7 月 n=670，1.33、0.551。以標準差約 13 分估算，泡泡的標準誤約 1.0、2020-21 12～2 月約 0.58，所以泡泡與停賽前的差（約 0.8）不顯著，2020-21 前段與常態（約 2.1～2.7）的差（約 1.2～1.7）約 2～3 個標準誤 [我的估算，不是 SQL 結果]。
- ESPN 每場有 `gameInfo.attendance`，且 2020-21 的值有資訊量 [抽樣 n=約 20 場，ESPN API]：泡泡與 2020-12／2021-01 多為 0；2021-01-15 CLE 1944；2021-03-15 CHA 2861、BKN 1637、WAS 0；2021-05-15 BKN 1773、NYK 1981、IND 0；2021-11-05 起約 14,000～16,000；2026-01-20 約 17,800～18,500。**DB 目前沒有存這個欄位。**

### 3.7 現有程式如何使用這些資料（影響範圍）
- `jobs/features.py` 只讀 `games_v2`、`team_game_stats`、`player_game_stats`（`load_bundles`，`:362-417`）；**不讀** `player_game_status`、`game_officials`、`team_shot_buckets`、`market_lines` 的 `price_point`。
- 目前上線模型的訓練資料是 3 季（2023-24～2025-26）[總體規劃 §4.2 的說法，我沒有另外驗證]；2015-16～2022-23 還沒進入任何訓練。所以佔位 box 等問題目前**不影響上線模型**，但 P4／P5 會直接碰到。
- `webui` 的球員場均與 `jobs/diagnose.py` 讀 `player_game_stats`；佔位列（分鐘 NULL、數字 0）在舊季存在，網站只顯示最新球季，所以目前看不到影響。

## 4. 檢查清單

嚴重度：
- **error**：資料結構或內容明顯錯誤，會讓下游算出錯的結果；新的 error 讓檢查入口以非 0 結束。
- **warn**：有已知原因與已知範圍的缺陷；超出基準線才失敗。
- **info**：只回報數字，不判成敗。

每項檢查回傳零或多筆 `Finding`（§7.1）。「範圍」欄是檢查的維度（季、場、隊場）。「已知基準」是 §3 查到的現況；其中**門檻**是建議值，實作時用檢查入口對正式 DB 跑一次（唯讀）確認基準，再寫入基準線檔。

### 4.1 場次（A 類）
| id | 嚴重度 | 判斷 | 門檻 | 已知基準 |
| :- | :- | :- | :- | :- |
| A1 `espn_event_coverage` | error | 對指定日期重新抓 ESPN scoreboard，數「NBA 對 NBA、已完賽」事件，與 DB 同日同類型場數相比；被略過的事件分類 | 差異 ≠ 0 即 error；被略過事件的類型不在白名單（全明星賽、季前賽的非 NBA 隊、`TBD`）即 error | 12 季差異 0（§3.1） |
| A2 `team_game_counts` | warn | 每隊例行賽場數：預期 82（2020-21 為 72；2019-20 不檢查）；2023-24 起允許恰好 2 隊為 83（NBA Cup 冠軍賽參賽隊） | 不符預期即 warn；83 場的隊數 ≠ 2 即 error | 見 §3.1 |
| A3 `duplicate_games` | error | 同日同主客隊的場次數 > 1 | > 0 | 0 |
| A4 `team_twice_same_day` | warn | 同一隊同一 ET 日出現 2 場以上 | 超出基準線 | 1（2020-03-01 GSW，ESPN 資料錯） |
| A5 `stuck_not_final` | error | `game_date_et` 早於今天 3 天以上仍非 `final` | > 0 | 0 |
| A6 `final_without_score` | error | `final` 但比分或分差為 NULL，或平手 | > 0 | 0 |
| A7 `suspicious_tipoff` | warn | `tipoff_utc` 轉 ET 後小時 < 10，或為 NULL，或 ET 日期 ≠ `game_date_et` | 超出基準線 | 26 場（22 preseason、4 regular） |

### 4.2 box score（B 類）
| id | 嚴重度 | 判斷 | 門檻 | 已知基準 |
| :- | :- | :- | :- | :- |
| B1 `team_box_complete` | warn | 完賽場兩隊 `team_game_stats` 的 `fga、fgm、fg3a、fta、tov、oreb、dreb` 皆非 NULL | 超出基準線 | 佔位 322／336／344（2015-16～2017-18 regular＋playoffs 隊場）、12（2020-21 play-in）、14（2021-22 preseason）、10（2024-25） |
| B2 `player_box_not_placeholder` | warn | 每隊球員列中 `min_played` 非 NULL 的列數 > 0 | 超出基準線 | 同 B1（兩者在 2015-16～2021-22 完全重合；2024-25 的 10 隊場是只有 B1 失敗） |
| B3 `player_points_match_team` | warn | 球員得分加總 = 球隊得分（僅對 B2 通過的隊場） | 超出基準線；單場差 > 3 另記 | 2018-19 23、2025-26 14、2019-20 2、2020-21 1、2015-16 1 |
| B4 `team_minutes_240` | warn | 球員分鐘合計 ≈ 240＋25k（容許 ±3） | 超出基準線 | 2018-19 21、2025-26 23、2023-24 5、2021-22 4、2020-21 3、其餘 ≤ 2 |
| B5 `impossible_lines` | error | 球員列 `fgm > fga`、`ftm > fta`、`fg3m > fg3a`、`pts < 0`、`min_played > 60` | > 0 | 0（`min_played > 60` 有 1 列，需查） |
| B6 `team_points_equal_score` | error | 球隊 `pts` ≠ 比賽比分 | > 0 | 0 |

### 4.3 先發、裁判（C 類）
| id | 嚴重度 | 判斷 | 門檻 | 已知基準 |
| :- | :- | :- | :- | :- |
| C1 `status_rows_present` | error | 完賽場有 `player_game_status` 列 | > 0 場缺 | 2025-26 季後賽 8 場 |
| C2 `starters_are_ten` | warn | 每場先發恰 10 人 | 超出基準線 | 2018-19 19 場、2025-26 8 場（同 C1）、其餘 ≤ 1 |
| C3 `officials_at_least_three` | info | 裁判 ≥ 3 位 | 只回報 | 2020-21 40、2023-24 19、2022-23 17、2025-26 15、2021-22 14、2018-19 4 |

### 4.4 出手桶（D 類）
出手桶**不用規則去修**。理由：2025-26 的多出筆數我試過規則只能解釋約一半（§3.3）；而差異只有 1～2 筆（約 1～2% 的出手），對「各桶占比」的影響很小。處理方式是**分類並記錄**，讓 P4 自己選擇。

分類（對每個隊場次，`d = 出手桶 FGA 加總 − box FGA`）：
- `exact`：`d = 0`；
- `minor`：`|d| ≤ 2`（且 `d ≠ 0`）；
- `major`：`|d| > 2`；
- `none`：沒有出手桶列；
- `no_box`：box 的 `fga` 為 NULL（無法比對，歸 B1）。

| id | 嚴重度 | 判斷 | 門檻 | 已知基準 |
| :- | :- | :- | :- | :- |
| D1 `shots_present` | error | 完賽場有出手桶列（有球員 box 的前提下） | > 0 場缺 | 2025-26 季後賽 8 場（16 隊場） |
| D2 `shots_class_share` | warn | 每季每類型 `exact` 比例與 `major` 比例；**分母只計可比對的隊場（`no_box` 不計）**；可比對場次少於 200 場的範圍只回報（info） | 比基準線退步 > 3 個百分點 | exact（regular，可比對分母）：2015-16 52%、2016-17 93%、2017-18 95%、2018-19 96%、2019-20 97%、2020-21 98%、2021-22 97%、2022-23 98%、2023-24 98%、2024-25 98%、2025-26 62%、2026-27 60%（n=72，不足 200，僅回報） |
| D3 `shots_3pa_match` | info | 出手桶 3PA 加總 = box 3PA | 只回報 | 2025-26 regular 936 隊場不符 |
| D4 `shots_unknown_share` | info | `dist_bucket='unknown'` 的出手占比 | 只回報（P4 用「已知出手」當分母） | 我抽樣有座標的出手約 78%～86%，含罰球分母，所以實際略高 [抽樣 n=4 場] |

2025-26 起 plays 多於 box 的現象：D2 對 2025-26 以後用另一組基準（`minor` 為常態）；若 `major` 比例明顯上升（> 2%，目前 2025-26 regular 為 21／2462＝0.9%）才升級。

### 4.5 盤口（E 類）
**使用哪一列盤口**：所有 E 類檢查與 `v_game_quality` 的盤口欄位一律用既有的 `v_closing_lines`（專案的收盤線規則：非 manual、`captured_at ≤ tipoff_utc`、依 book 排序後取最新一筆；P5 也會用它），每場至多一列。`line_provenance` 取該列的 `price_point`（讓分），`total_provenance` 取該列的 `total_price_point`（總分），兩者分開，因為同一列的讓分與總分可能來自不同時間點。2026-27 同時有 `close` 與 `current` 列：以 `v_closing_lines` 選出的那一列為準。

| id | 嚴重度 | 判斷 | 門檻 | 已知基準 |
| :- | :- | :- | :- | :- |
| E1 `line_coverage` | warn | regular／playoffs／playin 的完賽場沒有任何 ESPN 盤口 | 超出基準線 | 2018-19 4、2021-22 7（preseason 幾乎都沒有，不計） |
| E2 `spread_range` | error | `abs(home_spread) > 25` 或 NULL | > 0 | 0 |
| E3 `spread_health` | error | 每季 regular：讓分與賽果相關 ∈ [0.30, 0.65]、MAE ∈ [8.0, 12.0]（賽後污染會使相關逼近 1） | 超出範圍即 error | 相關 0.409～0.532、MAE 9.06～10.83 |
| E4 `total_range` | error | 總分線 ∉ [170, 280] 或 NULL（有線但無總分另記 info） | > 0（先排除基準線內已知壞資料） | 2021-22 24 場（49.5～60）、2015-16 1 場（2001）、2017-18 1 場（−1） |
| E5 `total_health` | warn | 每季 regular：總分線與總分相關 ≥ 0.25，平均誤差絕對值 ≤ 2 | 排除 E4 旗標後仍不符才 warn | 全量未排除時：2015-16 相關 0.068、2021-22 相關 0.146／誤差 +3.62；**排除後是否恢復尚未驗證** |
| E6 `price_point_provenance` | info | 各季 `price_point` 分布；NULL 列是否全為 `captured_at = tipoff_utc` | 只回報；若出現 `captured_at ≠ tipoff_utc` 的 NULL 列即 warn | 2023-24～2025-26 NULL 列全部 `captured_at = tipoff_utc` [全量，S9] |
| E7 `line_flip_check` | error | 讓分符號慣例：`corr(margin, −home_spread)` > 0 | ≤ 0 即 error | 全季為正 |

### 4.6 情境旗標（F 類，不是成敗檢查，是 `v_game_quality` 的欄位）
- `is_cup_final`：NBA Cup 冠軍賽（ESPN 標成 regular，不計入每隊 82 場）。已知 3 場以明列 game_id 內嵌在檢視表；2026-27 的冠軍賽打完後由 A2（有 2 隊為 83 場）偵測並提醒人工補列。
- `is_bubble`：`game_date_et` 介於 2020-07-30 與 2020-10-12（含復賽的 regular、季後賽，n=172 場）。n=172 是 S4b 這組日期條件的實際結果 [全量，日期範圍是 SQL 自己的定義]；「泡泡賽實際就在這個區間」是依我對賽程的記憶 [推測]，建議實作時以 `game_date_et` 的最早／最晚值核對。
- `crowd_level`：目前**沒有資料**。建議新增 `games_v2.attendance`（§9 P3b）；在那之前用日期近似：2020-12-22～2021-05-31 為 `restricted`（我抽樣顯示該段多為 0 或約 2,000，但各隊不一，所以只是近似 [抽樣]）。
- `suspicious_tipoff`：同 A7。

## 5. 檢視表 `v_game_quality`

**第一版不建檢視表，只做唯讀函式**：`jobs.quality.game_quality(run, scope) -> DataFrame`，用一段 SELECT（CTE）算出下表所有欄位，每場一列，欄位與意義相同。理由：把檢視表放進 `jobs/schema.py` 的 `VIEWS` 會在下一次任何寫入 job（`ensure_schema` 每次都跑 `VIEWS`，包含每日排程）時於正式 DB 建立，等於 P3 偷偷寫正式 DB，也讓每日流程暴露在一個慢或壞掉的檢視表之下。

**第二版（P3a.2，需要使用者當下同意）**：同一段 SELECT 包成 `CREATE OR REPLACE VIEW public.v_game_quality`，**不放進 `ensure_schema` 的 `VIEWS`**，改由明確指令 `python -m jobs.quality --create-view` 建立（寫正式 DB）；建立前先用 `EXPLAIN` 與計時確認效能，不升 `SCHEMA_VERSION`。不改任何現有表。若效能不佳，保留按季查詢的函式作為正式介面。

| 欄位 | 型別 | 定義 |
| :- | :- | :- |
| `game_id`、`season`、`season_type`、`game_date_et` | | 取自 `games_v2` |
| `team_box_ok` | bool | 兩隊 `team_game_stats` 皆在，且 B1 的七個欄位皆非 NULL |
| `player_box_ok` | bool | 兩隊球員列中 `min_played` 非 NULL 的列數皆 > 0 |
| `points_consistent` | bool | 兩隊球員得分加總 = 球隊得分（`player_box_ok` 為假時為 NULL） |
| `shots_class` | text | `exact／minor／major／none／no_box`（§4.4，取兩隊中較差者） |
| `shots_diff_max` | int | 兩隊 `abs(d)` 的最大值 |
| `status_ok` | bool | `player_game_status` 有列且先發恰 10 人 |
| `officials_n` | int | 裁判數 |
| `has_spread`、`spread_ok` | bool | 有 ESPN 讓分且 E2 通過 |
| `has_total`、`total_ok` | bool | 有總分線且 E4 通過 |
| `spread_provenance`、`total_provenance` | text | 取自 `v_closing_lines` 該列的 `price_point`、`total_price_point`：`close／current／open／backfill_unlabeled／none`（NULL 且 `captured_at = tipoff_utc` 者為 `backfill_unlabeled`） |
| `is_cup_final`、`is_bubble`、`crowd_restricted`、`suspicious_tipoff` | bool | §4.6 |
| `usable_team_features` | bool | `status='final' AND team_box_ok` |
| `usable_player_features` | bool | `usable_team_features AND player_box_ok AND points_consistent` |
| `usable_shot_features` | bool | `shots_class IN ('exact','minor')` |
| `usable_spread_line` | bool | `has_spread AND spread_ok` |
| `usable_total_line` | bool | `has_total AND total_ok` |

`usable_*` 是**建議的預設篩選**，P4／P5 可以覆蓋，但覆蓋要在自己的設計中寫明理由。檢視表本身不做加權。

第二版建立檢視表之前，以 `EXPLAIN` 與計時確認在 DB 目前大小（完賽約 1.6 萬場、球員列 32.6 萬）下回應時間可接受；若太慢，改成按季查詢（檢視表加 `WHERE season = ...` 由呼叫端帶）。**這一點未驗證。**

## 6. 對 P4／P5 的建議（明確結論）

### 6.1 歷史盤口能不能訓練 P5 第二層「對照盤口」
**可以，但有明確範圍與保留：**

1. **讓分線可以當「近似收盤線」使用，全部 12 季**，限 `usable_spread_line`。證據：(a) 各季與賽果的相關 0.409～0.532、MAE 9.06～10.83，完全是賽前線的樣子，沒有賽後污染 [全量]；(b) 2023-24 起 ESPN 的 `current` 與 `close` 在我抽的 31 場完全相同，且 2015-16～2022-23 只提供 `current`，所以舊季的值是「ESPN 最後一筆報價」，與收盤線一致的可能性高 [抽樣 + 推測]。
2. **不能宣稱它們是「收盤時刻」的報價**：沒有真實時間戳，只有 `captured_at = tipoff_utc` 的人為填值。因此：
   - **CLV（與收盤線的差）不能用歷史資料算**。CLV 只能用 P1 上線（2026-10-24）之後累積的即時快照；以每天約 10 場估算，約 3 個月才有約 1,000 場，**這個時程是估算**。
   - 沒有開盤線（2023-24 之前 ESPN 沒給），所以「從開盤到收盤的移動」特徵在歷史資料不可得。
3. **總分線要先排除壞資料**：`usable_total_line`（E4）排除 2021-22 的 24 場、2015-16 的 1 場、2017-18 的 1 場。排除後總分相關是否恢復到 0.35 以上，**尚未驗證**，實作 E5 時第一次執行就能得到答案。
4. **供應商年代漂移風險**：`book` 欄位一律是 `'espn'`（CLAUDE.md 規定不改，因為 `v_closing_lines` 依 book 排序），所以看不出各年代是哪一家供應商的線。建議在 P5 前先對即時快照新增 `provider` 欄位（只新增、舊列為 NULL，§9 P3b），從現在起保存；歷史資料用「各季讓分線與賽果的平均偏差（`avg(margin + spread)`）是否接近 0」當漂移的體檢——這個數字 S3c 沒有取，建議實作時補在 E3。
5. 只用 2026-10-24 起的真實時間戳盤口訓練第二層？**不建議**：樣本太少（P1 於 2026-10-24 上線，截至 2026-10-25 約 1 天），且歷史線已通過體檢。可以用「歷史訓練、即時快照當驗證」的切分，這也是最誠實的做法。

### 6.2 2020 泡泡賽與疫情兩季
**不排除，加情境特徵；由 P5 的 walk-forward 驗證決定是否需要降權。**
- 泡泡 172 場與 2020-21 前段 501 場的主場優勢確實較低（§3.6），但樣本不大、差異部分在 1～3 個標準誤之間，排除會損失約 700 場訓練資料。
- 作法：`v_game_quality.is_bubble` 與 `crowd_level`（有 attendance 後用連續值，之前用日期近似）作為模型輸入或主場優勢特徵的調整項；P5 做「含／不含這些場次」「降權 0.5」的 walk-forward 比較，若無差異就保留全部。
- `neutral_site` 不能代表泡泡（ESPN 回 False），所以泡泡一定要靠日期旗標；P4 設計時要寫進去。
- NBA Cup 冠軍賽：保留，標 `is_cup_final`（中立場地、不計入戰績場數）；P4 的「隊伍已打場數」類特徵要決定是否把它算進去。

### 6.3 出手桶
- 只用 `usable_shot_features`（`exact` 或 `minor`）的場次；**其餘當缺值**，不插補。目前模型用的 `HistGradientBoostingRegressor`（`jobs/model.py:22,58`）原生支援 NaN；若 P5 加 Ridge 或深度學習，要自己處理缺值（插補或加缺值旗標）。
- 用**占比**（各桶出手數 ÷ 已知出手數）而不是絕對數，因為差異只有 1～2 筆、且 `unknown` 桶占約 10%～20%。
- 季別建議：2016-17 以後全面使用 `usable_shot_features`（`exact` 加 `minor` 涵蓋可比對隊場的 99% 以上，2016-17、2017-18 的 `major` 幾乎為 0）；**2015-16 只用 `exact` 與 `minor`（約 75% 的可比對隊場），`major`（約 25%，plays 平均少 1.5 筆以上）當缺值**。缺的 play 是否隨機（會不會集中在特定節或出手類型）**我沒有查**，P4 若要用 2015-16 應先查。
- 這個結論依賴「舊季 plays 不完整」這個推測；若要在 P4 實際使用 2016-17／2017-18，應先檢查「缺的 play 是否隨機」（例如缺的是否集中在特定節或特定出手類型），**我沒有做這個檢查**。

### 6.4 佔位 box（2015-18 約 12% 的場次等）
- 這些場次的球隊層與球員層特徵**都不可用**（§3.2），而 `features.py` 會靜默略過它們（不報錯）。P4 要明確選擇：(a) 訓練時直接排除 `usable_team_features = false` 的場次當「更新來源」與「訓練列」；或 (b) 保留為訓練列但特徵標記為缺值。**建議 (a) 用於更新來源，(b) 不採用**——因為這些場次的賽前特徵本身可能是好的（來自更早的場次），只是賽後不能用來更新球隊狀態；實務上 `features.py` 目前就是這樣做（略過更新、保留場次計數），所以預設行為可接受，但**要讓 `gp`（已打場數）不要把佔位場次算成「有資料的場次」**，否則「樣本數足夠」的暖機判斷（`MIN_GP_OTHER_SEASONS`）會高估。這一點留給 P4。
- 2024-25 的 10 個隊場、2025-26 的 8 場季後賽缺資料，量小，直接用 `usable_*` 旗標排除即可。

### 6.5 未知縮寫略過的事件
全部確認為合理略過（全明星賽、季前賽國際球隊、TBD 佔位）[§3.1]，**沒有應該補收的**。維持「未知縮寫該場略過並印 WARN」的規則；A1 負責確保「被略過的事件類型不在白名單」時報錯，防止將來出現新的 NBA 球隊縮寫（例如球隊更名或擴編）而被靜默略過。

## 7. 檢查入口（設計；P3 本身不寫程式）

### 7.1 結構
```
jobs/quality/__init__.py      # 匯出 run(), Finding, REGISTRY
jobs/quality/checks_games.py  # A 類
jobs/quality/checks_box.py    # B、C 類
jobs/quality/checks_shots.py  # D 類
jobs/quality/checks_lines.py  # E 類
jobs/quality/baseline.json    # 已知問題基準線（§7.3）
jobs/quality/__main__.py      # CLI
tests/test_quality_*.py       # 用 tests/fakes.py 的 FakeDb 風格；不連 DB、不打網路
```
- `Finding`：`check_id: str`、`severity: Literal["error","warn","info"]`、`scope: str`（例如 `2025-26/regular`）、`n_bad: int`、`n_total: int`、`detail: dict`（含最多 20 個範例 game_id）、`baseline: int | None`、`status: Literal["ok","known","new","worse"]`。
- 檢查函式介面：`def check_xxx(run: Run, scope: Scope) -> list[Finding]`，其中 `Run` 與 `webui/queries.py` 相同（`run(sql, params) -> DataFrame`），`Scope` 為 `(seasons: list[str] | None, since: date | None)`。**所有 SQL 只含 SELECT**，並沿用 `webui/queries.py` 的測試（掃描寫入關鍵字）。A1 是唯一會打 ESPN 的檢查（只讀 GET），預設只在 `--with-espn` 時執行，因為它需要網路。
- 連線：沿用 `webui/build_site.py` 的 `connect_readonly()`（唯讀 session、逾時、不洩漏連線字串）。

### 7.2 CLI
```
python -m jobs.quality [--seasons 2025-26,2026-27 | --all] [--since 7d]
                       [--with-espn] [--fail-on error|warn|none]
                       [--json out.json] [--baseline jobs/quality/baseline.json]
```
- 預設輸出一張表（檢查 id、範圍、不良數／總數、狀態），結尾列出所有 `new` 與 `worse`。
- 結束碼：有 `status in (new, worse)` 且嚴重度達 `--fail-on` 即非 0。
- 寫入類動作：**沒有**。

### 7.3 基準線（避免永遠紅燈）
- `baseline.json` 記錄每個「檢查 id＋範圍」的已知不良數與一行原因，例如 `{"B1": {"2015-16/regular": {"n_bad": 322, "why": "ESPN 佔位 box"}}}`。
- 基準線的鍵是 `(檢查 id, 季, 季別類型)`；§4 表格中寫成單季總數的基準，實作時要拆成季別類型（regular／playoffs／playin／preseason）。檔頭記錄 `as_of_date`（產生基準線的日期）。
- 狀態：不良數 ≤ 基準 → `known`；> 基準 → `worse`；基準沒有的範圍出現不良 → `new`；不良數 < 基準（資料被修好）→ `ok`，並提示「可以降低基準線」。**結束碼只看 `new` 與 `worse`**：已知的非零基準（例如 B5 有 1 列 `min_played > 60`）即使嚴重度是 error，狀態是 `known`，結束碼為 0。
- **區間執行（`--since`、每日）**：只評估 `game_date_et > as_of_date` 的場次；這些場次是基準線之後的新資料，出現任何不良一律 `new`。基準線之前的場次由完整執行負責，區間執行不拿局部數字去比整季基準（否則幾乎都會顯示 ok／known）。
- **比例型檢查（D2、E3、E5）與進行中的球季**：某範圍可比對的 regular 場次少於 200 場時只回報（info），不判成敗。例如 2026-27 目前 n=11 時 MAE 6.23、相關 0.702 會超出 E3 的範圍，這在樣本不足時是預期的，不是資料問題。
- **基準線不是手抄 §3 的數字**：實作者先用檢查入口對正式 DB 跑一次（唯讀）產生初稿，與 §3 逐項比對；不一致要找出原因，不能直接採用較大的數字。
- 修改基準線必須在 commit 訊息說明原因，並讓 `reviewer` 看到（CLAUDE.md 對 gates 的「不為了通過而放寬」同樣適用）。

### 7.4 執行時機
（以下是 P3b 的範圍，見 §9；P3a 只交付模組、CLI、基準線與測試，不改任何 workflow。）
1. **每次回補後**：`workflow_dispatch` 新增 `v2_quality`（唯讀，需要使用者當下同意才能觸發，與其他 dispatch 相同）；或由主線在本機用唯讀連線執行（目前最簡單的做法，不需要改 workflow）。
2. **每日**：在 `run_daily` 之後加一個獨立步驟 `python -m jobs.quality --since 7d --fail-on error`（只看最近 7 天，範圍很小，A1 不執行）。**建議不要併進 `run_daily` 的步驟清單**（避免品質檢查失敗讓整個每日流程退出碼為 1 並遮住資料抓取本身的紅燈），改為 workflow 內獨立 step，`if: always()`。這個取捨留給實作者與使用者決定。
3. **新增球季或換 ESPN 欄位時**：`--all --with-espn` 完整跑一次。

### 7.5 測試
- 每項檢查至少一個「資料正常 → ok」與一個「資料有問題 → 回報」的測試（假 DB）。
- 基準線狀態機（`known／new／worse／ok`）的邊界測試。
- `v_game_quality` 的欄位定義用小型假資料在 SQL 層面無法測試（沒有真 Postgres）；改為：把每個旗標的判斷寫成純 Python 對照實作＋同一組假資料的期望值，檢視表的 SQL 則在使用者同意後對正式 DB 以 `EXPLAIN` 與抽樣比對驗證，**這一步標為未驗證，直到做完**。

## 8. 已知問題清單（基準線初稿；全量數字，來源見 §3）

| 問題 | 範圍 | 規模 | 影響 | 建議處置 |
| :- | :- | :- | :- | :- |
| 佔位 box（球隊與球員統計為 NULL／0） | 2015-16～2017-18 regular／playoffs、2020-21 play-in、2021-22 preseason | 322＋336＋344＋12＋14 隊場 | 該場無球隊／球員特徵 | `usable_*` 旗標；不重抓（ESPN 現在仍是佔位，§3.2） |
| 球隊統計 NULL 但球員 box 正常 | 2024-25 | 10 隊場 | 該場無球隊特徵 | 先重跑 `ingest_espn` 看 ESPN 是否已補（§9 P3c），仍缺就旗標 |
| 球員得分加總 ≠ 球隊得分 | 2018-19 23、2025-26 14、其他 ≤ 2 | 隊場 | 球員特徵可疑 | 旗標 `points_consistent` |
| 出手桶少於 box（舊季） | 主要是 2015-16 | exact 52%（2016-17 93%、2017-18 95%） | 出手占比偏 | `shots_class` 旗標 |
| 出手桶多於 box 1～2 筆 | 2025-26 起 | exact 約 60% | 占比影響很小 | 容忍，不修；記錄 diff |
| 沒有出手桶與先發資料 | 2025-26 季後賽 8 場 | 8 場 | 缺 | 重跑 ingest 看 WARN（§9 P3c） |
| 總分線壞值 | 2021-22 24 場、2015-16 1 場、2017-18 1 場 | 26 場 | 總分模型 | `usable_total_line` 旗標；修補見 §9 P3c |
| 讓分線 `price_point` 為 NULL | 2023-24～2025-26 | 4,157 列 | 時間點未知 | 視為 close（推測，§6.1），旗標 `backfill_unlabeled` |
| ESPN 開賽時間可疑 | 2019-20 等 | 26 場 | 低（主要是 preseason） | `suspicious_tipoff` 旗標 |
| 泡泡未標中立、疫情主場優勢 | 2019-20、2020-21 | 約 700 場 | 主場優勢特徵 | `is_bubble`、`crowd_level` |

## 9. 後續項目（各自獨立，皆需另走計畫與使用者同意；P3 本身不做）

- **P3a**：實作 `jobs/quality`（含 `game_quality()` 函式與 CLI）、基準線、測試（只讀，不改資料、不改 workflow、不建檢視表）。這是 P3 的主要交付。
- **P3a.2**：`python -m jobs.quality --create-view` 建立 `v_game_quality`（寫正式 DB，需使用者當下同意，建立前先 `EXPLAIN`）。
- **P3b**：workflow 接線：`v2_quality` 的 `workflow_dispatch`、`run_daily` 之後的獨立每日 step（需同步更新 `tests/test_daily_workflow.py`）；觸發與啟用需使用者同意。
- **P3b**：新增欄位（只新增、可重複執行）：`games_v2.attendance`（從 `gameInfo.attendance` 解析，重跑 ingest 回填）、`market_lines.provider`（只對新的即時快照寫入）。小改動，但會寫正式 DB。
- **P3c**：修補嘗試（寫正式 DB，逐項同意）：(1) 重跑 2025-26 季後賽 8 場與 2024-25 的 10 隊場，看 ESPN 是否已補、或看 WARN 找原因；(2) `parse_close_line` 對「總分線不合理（不在 [170, 280]）」的供應商改試下一家供應商的總分（例外於「總分與讓分同一家」的規則，需 `reviewer` 審）並重抓 2021-22 的 24 場。
- **P3d（選配）**：A1 的 ESPN 覆蓋檢查改成每季完成後自動執行。

## 10. 對這份設計的限制與未驗證

- 所有「門檻」都是建議值，要等 P3a 用檢查入口對正式 DB 跑出基準後才定案。
- `v_game_quality` 的 SQL 尚未寫出，也未對真實 Postgres 驗證（效能與正確性）。
- 2025-26 起 plays 多於 box 的原因、舊季 plays 缺少的原因、2018-19 與 2025-26 的得分／分鐘不符原因，都**未查明**；設計選擇不依賴原因（分類並記錄）。
- 舊盤口是 close 的推論只靠 31 場抽樣與各季的體檢；沒有全量證據。
- ESPN 的 `current`＝`close` 觀察只涵蓋 2023-24 之後；2015-16～2022-23 的 `current` 是否為最後報價是推測。
- 我（支線）看不到 DB，所有 DB 數字都來自主線轉貼的 SQL 結果檔；抽樣數字來自我直接對 ESPN 的唯讀 GET。
