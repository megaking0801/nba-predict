# 預測比賽：合法、又直接用到「校準機率＋Brier」的出口（2026-10-28，查證，不是實作）

背景：專案本身的回測顯示沒有任何下注玩法賺錢；P4／P5 練出來的能力是**誠實評估機率**（walk-forward、校準、Brier、配對 bootstrap、事前登記）。預測比賽正是用這種能力評分、發獎金，不是賭博、不必賣明牌，也不碰 ESPN 資料（主辦方提供資料）。
方法：WebSearch／WebFetch 讀公開資訊。**Kaggle 的官方頁面是 JavaScript 動態載入，WebFetch 讀不到內文**，所以下面 Kaggle 的數字多數來自二手來源（得獎者的公開解題、往年規則頁的搜尋摘要），標示處要再到官方頁面確認。「未驗證」＝我沒有找到可靠來源。

## 1. Kaggle March Machine Learning Mania（NCAA 男女籃錦標賽勝率預測）
| 項目 | 查到的內容 | 可信度 |
| :- | :- | :- |
| 在比什麼 | 預測 NCAA 男、女錦標賽每一種可能對戰的勝率；用 **Brier 分數**評分（越低越好，全部 50/50 是 0.25） | 得獎者與參賽者的公開文章 |
| 2026 規模 | 3,485 隊；冠軍 Brier 0.1097（126 場計分），第 21 名 0.1204 | 冠軍公開解題（GitHub）、第 21 名部落格 |
| 獎金 | 2025 官方頁的搜尋摘要：總額 50,000 美元，第 1 名 10,000、第 2 名 8,000、第 3 名 7,000、第 4～8 名各 5,000；2026 冠軍解題寫 10,000 美元、第 2 名 8,000、第 4～8 名各 5,000 | 二手，**官方 2026 規則未讀到** |
| 時程 | 每年 3 月（選拔賽程公布後到開打前截止）；2026 的截止日一個個人專案寫 3 月 19 日 | 二手，**2027 是否續辦、日期未驗證** |
| 外部資料 | 往年規則：允許「免費、公開」的外部資料（2024 頁面）；2017 規則要求外部資料在當季開打前就可取得。2026 冠軍用了 AP 排名、RotoWire 傷兵、EvanMiya 球員效率（手動輸入），**沒用賭盤** | 二手；**2026／2027 的確切條款未驗證** |
| 得獎義務 | 2022 規則摘要：得獎者授權為開源；資料只限比賽用途 | 二手，2027 未驗證 |
| 台灣居民可否領獎 | **未驗證**（沒讀到 Kaggle 的資格條款；Kaggle 一般排除受美國制裁國家，台灣不在常見清單內是我的認知，不是查證） | — |
| 稅 | 美國與台灣**沒有正式租稅協定**；《美台雙重課稅減免法》眾議院 2025 年通過，參議院仍在委員會（2026 年 9 月的資料）。非美國居民領美國來源獎金通常被預扣 30%、填 W-8BEN（一般知識與二手來源，**未驗證**）。台灣端的申報（海外所得、基本稅額）**未查**，要問會計師 | 部分查證 |

**跟我們的能力有多接近**：評分方式（Brier）、需要校準、需要防止過擬合，和 P4／P5 做的事幾乎一樣。冠軍的做法（隊伍強度差＋XGBoost＋isotonic 校準＋逐季留一驗證）和我們的管線同一類。**但資料是大學籃球**，我們的 NBA 管線不能直接用；要重新做資料處理與特徵（主辦方提供歷年比賽結果與種子）。

**誠實的期望值**：計分只有約 126 場，運氣成分很大；前 8 名占 3,485 隊的 0.23%。即使模型在前 5% 的水準，進前 8 名的機率我估計在幾個百分點以內（判斷，沒有數據）。所以期望收入大約是**幾百美元以下**，扣掉稅更少。它的價值主要是：(1) 一個公開、可驗證的排名，可以放進作品集；(2) 很好的練習場。

**冠軍一個值得記下的做法**：把 ≥97% 與 ≤3% 的預測推到 100%／0%，他承認這在期望上是負的，但能增加奪冠機率（獎金只給前幾名，鼓勵冒險）。這是比賽規則造成的策略，和「誠實校準」相反；要參賽就要先決定接不接受這種做法。

## 2. Metaculus 一類的預測錦標賽
| 比賽 | 查到的內容 | 可信度 |
| :- | :- | :- |
| Metaculus Cup | 2025 年推出，獎金池 5,000 美元（3,500 預測分數＋1,500 評論）；2026 Cup 本身的獎金**沒查到** | 論壇公告（二手） |
| FutureEval 機器人錦標賽 | 一年三季，每季 50,000 美元、300～400 題；2026 秋季**商業機器人不能領獎** | EA 論壇公告（二手） |
| Bridgewater × Metaculus 2026 | 30,000 美元獎金池 | Bridgewater 頁面標題（未讀內文） |
| ACX／Metaculus 2026 | 獎金 10,000 美元，**2026-01-17 截止，已結束** | Astral Codex Ten 文章 |
- 這類比賽題目是時事、科技、經濟，**和 NBA 無關**；比的是一般判斷力，或用 LLM 寫自動作答的機器人（FutureEval）。機器人路線要呼叫付費 LLM API（要花錢，須使用者同意）。
- 資格（國家）與稅務：**未驗證**。

## 3. 建議（讓使用者決定）
1. 如果使用者想試：**2027 年 3 月的 March Machine Learning Mania** 是最直接的。準備工作約 2～4 個週末（NCAA 資料處理、特徵、校準；可重用 walk-forward、Brier、bootstrap 的程式）。先做的一步：2027 年 2 月比賽頁上線後，讀官方規則確認三件事——台灣居民可否領獎、外部資料條款、稅務文件。
2. 不要把它當收入來源：期望值很小。當作作品集與練習。
3. Metaculus 類比賽和 NBA 無關，除非使用者本來就對時事預測有興趣，否則不建議。

## 來源
- [2026 冠軍公開解題（GitHub）](https://github.com/harrisonhoran/kaggle-march-mania-2026-1st-place/blob/main/kagglewriteup.md)
- [第 21 名心得（3,485 隊）](https://thomaslarock.com/2026/04/fail-better-then-finish-21st/)
- [March Machine Learning Mania 2025（官方頁，搜尋摘要）](https://www.kaggle.com/c/march-machine-learning-mania-2025)、[2024](https://www.kaggle.com/competitions/march-machine-learning-mania-2024/overview)、[2022 規則](https://www.kaggle.com/competitions/mens-march-mania-2022/rules)、[2017 規則](https://www.kaggle.com/c/march-machine-learning-mania-2017/rules)
- [Metaculus Cup 公告（5,000 美元）](https://forum.nunosempere.com/posts/XpdtiySZsHJmGL6dM/the-metaculus-cup-series-is-live-usd5-000-prize-pool)、[FutureEval 2026 秋季](https://forum.effectivealtruism.org/posts/hrJrsgMCwsdBuAqgQ/announcing-fall-2026-futureeval-bot-tournament)、[Bridgewater × Metaculus 2026](https://www.bridgewater.com/bridgewater-x-metaculus-2026-competition)、[ACX／Metaculus 2026](https://www.astralcodexten.com/p/acxmetaculus-prediction-contest-2026)
- [美台雙重課稅減免法現況（FAPA）](https://fapa.org/issues/u-s-taiwan-expedited-double-tax-relief-legislation)、[RSM 稅務快訊](https://rsmus.com/insights/tax-alerts/2025/house-passes-us-taiwan-double-tax-relief-act.html)
