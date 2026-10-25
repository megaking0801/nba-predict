# P4 特徵 v1 設計

日期：2026-10-26
狀態：草稿（支線 Opus 撰寫，待主線審、builder 查證事實）
上層文件：`docs/superpowers/specs/2026-10-23-roadmap-design.md` §6 P4
依據：`docs/superpowers/specs/2026-10-25-p3-data-quality-design.md`（`game_quality()` 的 `usable_*` 旗標）、`jobs/features.py` 現況、`tests/test_features_point_in_time.py`
範圍：只定特徵與驗證方法。這份文件不改任何程式；實作前要另寫計畫，並依 CLAUDE.md 進 plan mode（動到特徵與模型）。

## 1. 先講結論

1. **P4 的首要工作不是加特徵，而是資料範圍（P4.0）。** 現在的 `load_bundles()` 對所有 `final` 且有比分的比賽一視同仁（沒有 `season_type` 過濾，`jobs/features.py` 的查詢只看 `status='final' AND margin IS NOT NULL`），P2 補了 12 季後，季前賽、2020 泡泡、佔位 box 都進了特徵狀態。這些都要先量測、再決定怎麼處理。
2. 新特徵分六組，依「成本低、無洩漏風險小、資料品質好」排序做；每組在 walk-forward 用**配對比較**證明有用才保留（§6）。
3. **預計陣容（球員缺陣）在歷史資料上無法無偏地驗證**：歷史只有「實際上場名單」，serving 時只有「傷兵報告」。這組放最後，驗證結論要打折（§4.6）。
4. **裁判特徵先不做**：10:30 UTC 預測時，當天裁判多半還沒公布（時間點為推測，未驗證，§4.7）。
5. 誠實預期：特徵加進去，MAE 通常只改善 0.05–0.15 分（經驗值，**未驗證**）；市場收盤線比我們的模型準的機率很高。P4 的成功標準是「模型變準且可信」，不是「贏市場」（贏市場是 P5 的事）。

## 2. 現況與問題（事實，附出處）

| 事實 | 出處 |
| :- | :- |
| margin 模型 17 個特徵、totals 模型 18 個，版本字串 `fs1`／`ts1` | `jobs/features.py` 的 `FEATURE_NAMES`／`TOTAL_FEATURE_NAMES`、`jobs/config.py` |
| 狀態只吃球隊 box（四因素、ORtg／DRtg、pace）、球員 GameScore EWMA、Elo、休息日 | `FeatureBuilder.update` |
| `ENABLE_ALTITUDE_FLAG = False` 已存在於 config，但沒有任何程式讀它 | `jobs/config.py`（grep 確認：只在 `config.py` 出現，沒有任何程式讀它） |
| Elo 一律加主場優勢 `ELO_HCA=65`，不分中立場地 | `jobs/ratings.py` |
| 球季邊界會重置球隊狀態（`season_reset`），上季終值以 `PREV_SEASON_PRIOR_W=0.6` 帶入 | `TeamState.season_reset` |
| 佔位 box（統計 NULL/0）：2015-16 322、2016-17 336、2017-18 344 隊場，2020-21 play-in 12、2021-22 季前賽 14；`_team_game_metrics` 遇到 `fga<=0` 會回 None，所以這些場只更新休息日、場數、Elo，不進四因素 | `docs/handoff/p3-sql-results-2026-10-25.md`、`features.py` |
| 出手桶與 box 吻合率（exact）：2015-16 52%、2016-17 93%、2017-18 95%、2018-19 96%、2025-26 62% | P3 設計 §5 |
| 傷兵歷史：`injury_reports` 從 P1 上線（2026-10-24）才有球季中的資料，之前只有 2026-06-17 到 08-19 的休賽期快照 | 總體規劃 §4.2、roadmap |
| `predict_daily` 的特徵名單取自模型 payload（`feature_names`），不是模組常數 | `jobs/predict_daily.py:113` |
| walk-forward：最少 800 場訓練、每 14 天一折、最早一季調參、其餘為 report 段 | `jobs/evaluate.py` 的 `walk_forward`、`split_tune_report` |
| 使用者在台北晚上看網站；預測在 10:30 UTC（美東約 06:30） | 總體規劃 §6 P1 |

## 3. 無洩漏規則（所有新特徵必須遵守）

1. **先 emit、再 update**（既有規則）。任何新狀態只能在 `update(bundle)` 寫入，`emit()` 只讀。
2. **這場比賽自己的 box、比分、出手桶、上場名單、裁判、先發，emit 時一律不可見。** 唯一例外是「賽前就知道」的輸入：賽程（日期、主客場、場地）與下述的「已知缺陣名單」，它們必須由呼叫者明確傳進 `emit()`，不能從 bundle 偷取（§4.6）。
3. **賽程類特徵**（旅行、時區、密度、戰意）只用「這場之前」的賽程與戰績。戰績只算 `final` 且日期早於本場。同一天多場時，不可讓同日其他場的結果進入當日戰績（現有 `emit` 以日期排序，同日內順序固定；新特徵讀的是「日期嚴格早於本場」的狀態）。
4. 新增三個測試（見 §8）：標籤獨立性（改本場 box 與比分，本場特徵不變）、遮罩測試（改別場的缺陣名單，本場不變）、既有 golden 自動涵蓋所有新欄位（它用資料表的欄位清單，不是寫死的名單）。
5. **市場資料不當特徵。** 盤口（讓分、總分）在 P4 只用來評分，不進特徵；用收盤線當特徵屬於 P5 的第二層，且要另處理「預測當下看不到收盤線」的問題。

## 4. 特徵清單

欄位名是建議，實作時可調；「資料」欄指要新增的載入。優先序 1 最先。

### 4.1 P4.1 情境旗標與賽程（優先序 1，不需新表）

| 特徵 | 定義 | 資料 | 預期效果 |
| :- | :- | :- | :- |
| `neutral_game` | 中立場地（`neutral_site`）或泡泡日期（2020-07-30..2020-10-12）→ 1 | `games_v2.neutral_site`、日期區間（常數已在 `game_quality.BUBBLE_*`） | 主場優勢歸零；P2 已知 `neutral_site` 抓不到泡泡，所以要靠日期 |
| `crowd_restricted` | 2020-12-22..2021-05-31 → 1（近似，P3 設計已註明） | 日期 | 觀眾限制期主場優勢變小 |
| `is_playoffs`、`is_playin` | 賽別旗標 | `season_type` | 季後賽輪替與主場優勢不同；季後賽約占 6–7%（**估計**） |
| Elo 中立場修正 | 中立／泡泡場的 Elo 更新用 `hca=0`；更新時不把「主隊」當真主場 | `ratings.Elo.update(neutral=)` | 避免泡泡比賽污染評分 |
| `travel_km_diff` | 兩隊各自「上一場場地 → 本場場地」大圓距離（km／1000），主減客 | 新常數表 `TEAM_VENUE[abbr] = (lat, lon, tz, elevation_m)`（30 隊，逐隊核對） | 長途客場與連續客場較累 |
| `tz_shift_away` | 客隊上一場場地時區 → 本場時區（東向為正），以場地當地小時差計 | 同上 | 向東飛行較吃虧（假設） |
| `travel_km_diff_short_rest` | 只在休息 ≤1 天時才計 `travel_km_diff`，否則 0 | 同上 | 讓線性模型表達「短休息 × 長途」 |
| `road_trip_len_away`、`road_trip_len_home` | 連續客場場數（含本場） | 賽程 | 長途客場的累積疲勞 |
| `games_last7_diff` | 前 7 天內比賽數的主客差 | 賽程 | 取代部分 `three_in_four` |
| `altitude_edge` | `max(0, 本場場地海拔 − 客隊本場前主場海拔)/1000`；主要是 DEN（約 1,600 m）、UTA（約 1,300 m） | `TEAM_VENUE`；由既有 `ENABLE_ALTITUDE_FLAG` 控制 | 高海拔主場 |

說明：
- 中立場地比賽（巴黎、墨西哥城、盃賽決賽等）的場地不在 `TEAM_VENUE`。狀態不更新「最後場地」、旅行距離用最後已知的場地算，且以 `neutral_game` 旗標讓模型自己吸收。這種比賽一季不到十場（**估計**，實作時用 SQL 數 `neutral_site`）。
- `TEAM_VENUE` 數值**未驗證**：實作時逐隊核對，並加單元測試（距離對稱、LAL–BOS 約 4,180 km 誤差 2%、同城為 0）。座標與海拔屬公開常識但我沒有查證。
- 時區不用「球隊所在地」硬編，要用日期算（夏令時間），沿用專案既有的 ET 轉換方式；實作時先看 `jobs/` 現在用什麼（zoneinfo 或手算）。

### 4.2 P4.1b 戰意（優先序 2，需要全聯盟戰績狀態）

| 特徵 | 定義 |
| :- | :- |
| `gb_play_in_home/away` | 本場前，該隊與所屬分區第 10 名的勝場差（正＝領先），夾在 [−10, 10] |
| `eliminated_home/away` | 數學上已無法進前 10（最多可能勝場 < 第 10 名目前勝場）→ 1 |
| `clinched_home/away` | 數學上已確定前 6（目前勝場 > 第 7 名最多可能勝場）→ 1 |

- 只在例行賽最後 25 場內啟用，其餘為 0。2019-20（賽季被截斷、泡泡 8 場）停用（旗標全 0）；2020-21 的總場數是 72。
- 需要 `CONFERENCE`（東西區）常數表與每季總場數表 `SEASON_GAMES`。
- 風險：戰意效果（擺爛、休息先發）的大小**未驗證**；排序放後面，ablation 沒有進步就丟掉。

### 4.3 P4.2 四因素攻守拆分與投籃結構（優先序 3，只用球隊 box）

現在的 `efg10_diff` 等四個欄位是「（自己 − 對手）的主客差」。把它拆成攻（自己的值）與守（對手打我們的值）後，主客各有「攻擊 vs 對手防守」的優勢欄位：`edge_h = O(主) + D(客)`、`edge_a = O(客) + D(主)`（O、D 都以聯盟平均為基準的偏差，D 的符號使「守得好」為正）。**單看加總，等於現有欄位；改進來自攻守用不同的收縮強度**：守端的 eFG% 多半是對手運氣，應該收縮得更重（`SHRINK_K_DEF > SHRINK_K_OFF`，這是假設，**未驗證**，K 值由 ablation 在 tune 段決定）。

| 特徵 | 說明 |
| :- | :- |
| `efg/tov/orb/ftr` × `edge_h/edge_a`（8 欄） | 取代現有 4 個 diff 欄；攻守各自收縮 |
| `three_rate_edge_h/a` | 三分出手占比（`fg3a/fga`）的攻守對位；三分出手結構穩定，命中率不穩 |
| `ast_rate_diff` | 助攻占進球比（`ast/fgm`）主客差 |
| totals：`three_rate_sum`、`ft_rate_sum` | 大小分模型的水準／加總版本 |

- 資料：`team_game_stats`，已在 `load_bundles()` 載入（含 `fg3a`、`ast`）。聯盟平均用 point-in-time 的累計平均（既有 `_pace_sum` 的做法）。
- 只在 `usable_team_features` 的場次更新（佔位 box 本來就會被 `_team_game_metrics` 跳過）。
- 注意：三分出手占比與變異數有關（三分多＝波動大），這與 `HETERO_SIGMA`（異質 sigma，v1.1 旗標，需 Brier 贏 0.0005）有關；P4 只做均值特徵，異質 sigma 留給 P5。

### 4.4 P4.3 出手距離占比（優先序 4）

| 特徵 | 說明 |
| :- | :- |
| `rim_share`、`mid_share`（4–24 呎兩分）、`corner3_share`、`atb3_share` | 各隊進攻出手結構，30 場視窗、重收縮；另有對手被打的版本（守端） |
| 對位優勢 | 主隊攻 × 客隊守，同 §4.3 的做法 |

- 資料：`team_shot_buckets`，用 P3 的 `shots_class`（`exact`／`minor`）篩選；`major`、`none`、`no_box` 的場次**不更新狀態**（不補零）。2015-16 只有約 52% 吻合，該季大部分場次不會更新，特徵自然由先驗（聯盟平均）主導。
- 預期效果最不確定：eFG／三分占比已含大部分資訊。**只有在 P4.2 之後還有增量才保留。**
- 風險：2025-26 起出手桶比 box 多／少 1–2 次投籃（P3 §5 的觀察，原因未查）；不影響占比但會影響絕對數，這裡只用占比。

### 4.5 P4.4 球員進階數據（優先序 5）

- 現有球員價值是 GameScore／分鐘的 EWMA。新增 TS%、使用率（USG%）、助攻率的同類 EWMA（只在上場 ≥ `PLAYER_MIN_MINUTES` 的場次更新，與現有相同）。
- 隊級聚合：`rot_ts`（輪替前 8 人依上場分鐘加權的 TS%）、`usage_top2_share`（前兩名使用率占全隊比，代表明星依賴度）。
- 單獨這組的增量預期不大（GameScore 已含大部分）；它的主要用途是為 §4.6 的缺陣情境提供「缺了誰影響多大」的資訊，因此與 §4.6 一起評估。
- 資料：`player_game_stats`（已載入）。USG% 需要球隊分鐘與球隊出手／罰球／失誤，用同場的 `team_game_stats`。

### 4.6 P4.5 預計陣容強度（優先序 6，最後做；驗證要打折）

**問題**：模型要吃「這場誰不上場」。歷史資料（P2 的 `player_game_status`）只告訴我們「實際上場名單與沒上場原因」，那是**賽後才知道**的資訊（含臨場決定、傷勢變化）；serving 在 10:30 UTC 只有當時的傷兵報告（Out／Doubtful／Questionable／Day-To-Day）。用實際名單訓練、用報告預測，會有 train/serve 不一致，且 walk-forward 的成績會偏樂觀（球隊這場有沒有打明星球員，本身就和勝負相關，這是 outcome-adjacent 的資訊）。

**做法**：
1. `emit(home, away, date, unavailable=None)`：`unavailable` 是呼叫者提供的 `{team_abbr: {player_id: weight}}`（weight=缺陣機率），**函式內部不得從 bundle 讀本場名單**。
2. 歷史訓練：`unavailable` 取自 `player_game_status`，**只取 `did_not_play=True` 且原因屬傷病或休息類**（原因分布需先用 SQL 量，見 §9），權重 1；教練決定（Coach's Decision）不算，因為賽前不可知。標示為「oracle-OUT」。
3. serving：`unavailable` 取自 `injury_reports`（`reported_at` 在預測時間之前的最新一筆）；`Out`、`Doubtful` → 1 與 0.75；`Questionable`、`Day-To-Day` → 0.5（初始值，待 2026-27 累積後，用 `injury_reports` 對 `player_game_status` 實際出賽率校準；**這些權重目前沒有資料依據**）。
4. 特徵：`talent_loss_home/away`（缺陣球員在 `_full_strength` 中貢獻的份額總和）、`n_rotation_out_home/away`（輪替前 8 人中缺陣數）、`avail_strength_diff`。
5. 不一致的量測：等 2026-27 累積 ≥ 300 場同時有 `injury_reports` 與 `player_game_status` 的比賽後，重播這些場次，比較「用當時報告」與「用實際缺陣」的特徵值差異與 MAE 差異（oracle gap）。**在 oracle gap 量出來之前，這組只作研究、不進正式模型。**
6. 歷史 walk-forward 的 oracle-OUT 成績只當作**上限**。如果上限都沒有明顯進步，直接放棄這組；如果有，仍以 oracle gap 修正後的結論為準。

交易與名單變動：`roster_snapshots` 只從 2026-06 起有，歷史無法重建；「新加入球員的價值」靠球員 id 的 EWMA 自然跟隨（他第一次在新隊上場後就算進該隊的 `player_minutes`）。v1 不處理交易日當下的預先反映。

### 4.7 不做的（以及原因）

- **裁判特徵**：NBA 當天的裁判名單公布時間在預測之後的可能性高（我記得是比賽當天上午 9 點美東，**未驗證**），而 10:30 UTC 是美東 06:30。除非預測時間改到裁判公布之後，否則 serving 拿不到。P2 已存 `game_officials`，保留供日後使用；若要做，先驗證公布時間，再在 §6 的框架下評估（它主要影響總分與犯規）。
- **on/off、RAPM**：P7。
- **盤口當特徵**：P5。
- **深度學習**：P5。

## 5. P4.0：資料範圍與衛生（先於所有新特徵）

要回答的問題與做法：
1. **季前賽（`preseason`）、全明星、`allstar`**：不應進特徵狀態與訓練列（陣容與目的不同；季前賽的壞資料比例也最高，P3 baseline 的 D5 在 2021-22 季前賽高達 3.2%）。
2. **佔位 box 場次**：目前只是不更新四因素，仍計入場數／休息／Elo。這是正確的；但訓練列包含「特徵由很少場次支撐」的比賽，要量測（見 §6 的資料範圍實驗）。
3. **訓練起始季**：2015-16 到 2017-18 各約 13% 的隊場（322／336／344 對 2,460）是佔位 box，每隊 10 場窗口平均約有 1 場是空的，但各隊集中程度不同，要看分布。要比較「全 12 季」與「2018-19 起」。
4. **泡泡與觀眾限制**：用 §4.1 的旗標，不剔除。
5. **盃賽決賽**（3 場）：標成 `regular`，場地中立。用 `neutral_game` 吸收。

實作原則：這些變更全部放在 config 旗標後面（例如 `EXCLUDE_PRESEASON`、`TRAIN_FROM_SEASON`，預設維持現狀），讓 `fs1` 的輸出**逐位元可重現**；是否採用，由 §6 的實驗結果決定後，再刻意翻旗標並說明。

`load_bundles()` 要補的欄位：`neutral_site`、`tipoff_utc`，以及從 `game_quality()` 取得的 `usable_team_features`、`usable_player_features`、`usable_shot_features`、`is_bubble`、`crowd_restricted`、`is_cup_final`（P3 已建，CLAUDE.md 規定篩選資料用它）。`game_quality()` 的大型 CTE 在 `load_bundles` 路徑的效能**未驗證**（P3 驗證的是單獨執行）。

## 6. 怎麼證明「有沒有變準」

### 6.1 工具（新）：`jobs/feature_ablation.py`（唯讀）

為什麼要新工具：`python -m jobs.evaluate` 只跑目前的 `FEATURE_NAMES`，每次只能看一組特徵，而且 `main()` 只報 margin，沒有 totals，也沒有逐場配對。工具要做的事：
1. 載入資料一次、重播一次，`emit` 輸出**所有欄位的超集**，再用特徵集名單挑欄位；所有候選集都在**完全相同的列**上評估（eligible 列的交集）。
2. 每個候選集跑 `walk_forward`（ridge 必跑；hgb 與 blend 作為確認），輸出逐場預測。
3. 指標（全部是候選對基準 `fs1` 的配對差）：
   - `ΔMAE`、`ΔRMSE`（report 段與逐季）。
   - 配對 bootstrap 信賴區間：以 14 天為一區塊重抽（相鄰比賽相關），1,000–2,000 次，固定種子。
   - `Δ cover Brier`（有盤口的場次；與 `evaluate` 的 `cover_metrics` 同定義）與 `Δ 勝率 Brier`。
   - `info_over_market`（診斷，不是特徵）：對有收盤讓分的場次，迴歸 `margin + home_spread` 於 `mu − (−home_spread)`，看係數與信賴區間。係數顯著大於 0 才代表模型有市場沒有的資訊。這也是 P5 的核心指標，P4 先量基準值。
   - totals 另列：`ΔMAE`（總分）、與去掉壞值（P3 的 E4 範圍）後收盤總分的比較。
4. 輸出 JSON 與一頁文字表，存到 `docs/handoff/p4-ablation-*.json`。

### 6.2 流程與規則（事前登記）

資料切分：
- **調參／選特徵段**：2015-16（或 P4.0 決定的起始季）到 2024-25。
- **確認段（只看一次）**：2025-26 與 2026-27 已有的比賽。選定最終特徵集之前，不看確認段的任何數字；選定後只跑一次，結果照實公布，不再回頭挑。

順序（事前固定，不看結果換順序）：P4.0 資料範圍 → P4.1 情境與賽程 → P4.1b 戰意 → P4.2 四因素拆分 → P4.3 出手桶 → P4.4 球員進階 →（P4.5 另案）。每一組都對「累積到前一組為止的集合」做配對比較（累進式，不是各自對 fs1）。

保留規則（建議值，未驗證，實作前由主線與使用者確認）：
1. 調參段 `ΔMAE ≤ −0.02` 分，且 99% bootstrap 區間不含 0（共約 6 組，用 99% 近似多重比較校正）。
2. 逐季中至少 2/3 的季為負（同方向）。
3. `Δ cover Brier` 不為正（不變差）。
4. 沒達標就整組丟掉，不微調到達標（避免 p-hacking）；組內只調事前登記的超參數（收縮 K 的網格、視窗長度），且只在調參段。
5. 確認段若方向相反（變差），不採用該組，並寫進結論。

樣本數的概念：約 1.4 萬場時，配對 `|err|` 差的標準誤大約 0.01 分（**估計**，要實測 `|err|` 差的標準差），所以 0.02 分的改善在統計上可以偵測；但「統計上顯著」不等於「對下注有用」，後者只能由 P5 的 CLV／ROI 回答。

### 6.3 CLAUDE.md 要求的改前改後

1. **改前（現在就可以做，不需要改程式）**：`python -m jobs.evaluate`，貼出每個模型的 `[EVAL]`、`[BET]` 行與 JSON。注意：這是第一次用 12 季資料跑；`evaluate` 的 gates 是依舊資料設的，數字可能與舊的不同，這本身就是結論的一部分。
2. **改後**：`python -m jobs.feature_ablation --baseline fs1 --candidate <名稱>`（待實作），每一組各一份。

## 7. FEATURE_SET 版本怎麼升

- `features.py` 加 `FEATURE_GROUPS`（群組名 → 欄位名清單）與 `FEATURE_SETS = {"fs1": [...原樣凍結...], "fs2": [...]}`；`FEATURE_NAMES = FEATURE_SETS[CONFIG.FEATURE_SET]`；totals 同理（`ts1` → `ts2`）。
- **`emit()` 永遠輸出超集**；模型只挑名單內的欄位。這樣舊模型（payload 內有自己的 `feature_names`）在 `predict_daily` 照常運作，新舊特徵集可以並存到下次重訓。
- 實驗用的臨時集合用 `fs2-<群組>` 命名，只存在於 `feature_ablation` 輸出，不寫進 config、不進 DB。
- 最後只升一次：`FEATURE_SET: "fs1" → "fs2"`，`CONFIG_HASH` 因此改變。升版後 `evaluate` 的 gates（`GATE_MAE_ABS=10.5` 等）**不為了通過而調整**（CLAUDE.md）。
- 凍結測試：`test_fs1_names_frozen` 比對 `fs1` 名單，防止誤改。

## 8. 實作拆分與測試

| 步驟 | 內容 | 測試 |
| :- | :- | :- |
| P4.0a | `load_bundles` 補欄位與 `game_quality` 旗標；config 旗標（預設不變） | `fs1` 輸出與現況逐位元相同（用固定種子假資料） |
| P4.0b | `feature_ablation` 骨架（配對、bootstrap、`info_over_market`），先只比較 `fs1` 與自己 | 自己對自己的差為 0；已知人工差異可被偵測 |
| P4.1 | `TEAM_VENUE`、情境旗標、Elo 中立修正、旅行／時區／密度／海拔 | 距離與時區單元測試；標籤獨立性；golden |
| P4.1b | 戰績狀態、戰意 | 手算小聯盟的資料；2019-20 停用 |
| P4.2–P4.4 | 四因素拆分、出手桶、球員進階 | golden；佔位／`major` 場次不更新狀態 |
| P4.5 | `emit(..., unavailable=)`、缺陣特徵 | 遮罩測試；oracle gap 量測腳本（讀 `injury_reports`） |

既有的 `test_features_point_in_time.py` 用 `make_bundles()` 假資料，欄位是寫死的：新欄位（`neutral_site`、日期、出手桶、`unavailable`）要補進假資料，golden 才會涵蓋新特徵。

## 9. 需要主線用 DB 跑的東西（我沒有連線）

A. **改前基線**（指令，唯讀）：`python -m jobs.evaluate`，貼回完整輸出。
B. **SQL 量測**（MCP 唯讀即可；以下 SQL 未對真實 Postgres 驗證）：

```sql
-- 1) load_bundles 目前實際會吃進多少場，依賽別
SELECT season_type, count(*) FROM public.games_v2
 WHERE status='final' AND margin IS NOT NULL GROUP BY 1 ORDER BY 1;

-- 2) 中立場地與泡泡
SELECT season, season_type, count(*) FILTER (WHERE neutral_site) AS n_neutral, count(*) AS n
  FROM public.games_v2 WHERE status='final' GROUP BY 1,2 ORDER BY 1,2;

-- 3) 沒上場原因的分布（決定 oracle-OUT 要收哪些原因）
SELECT dnp_reason, count(*) FROM public.player_game_status
 WHERE did_not_play GROUP BY 1 ORDER BY 2 DESC LIMIT 40;

-- 4) 每季可用旗標比例：用 CLI 或 game_quality（需先在主線確認執行時間）
--    python -c "from jobs.quality.game_quality import game_quality ..."  （實作時補成輔助腳本）
```

C. **需要你（主線）決定／回報**：(1) §6.2 保留規則的數字是否合理；(2) `evaluate` 在 12 季資料上的 gates 是否仍通過（若已不通過，現在線上模型的啟用狀態要不要先處理）；(3) 預測時間是否有可能改到裁判公布之後（若可行，§4.7 的裁判特徵值得做）。

## 10. 風險與未驗證

- 本文所有門檻（ΔMAE −0.02、99% 區間、2/3 季、`SHRINK_K_DEF`、缺陣權重）是建議值，沒有任何實測。
- `TEAM_VENUE` 座標、海拔、時區與 `SEASON_GAMES` 要在實作時逐一核對。
- `game_quality()` 在 `load_bundles` 路徑的效能；12 季重播加上新狀態的耗時（工具要能在 GitHub runner 90 分鐘內跑完，或只在本機／主線 MCP 跑）。
- 預計陣容的 oracle gap 要等 2026-27 累積資料才能量，可能到 2027 年初。
- 戰意、旅行、海拔的效應大小是文獻印象，不是本專案的測量。
- 特徵增加會增加過擬合風險；`ridge` 有標準化與正則，`hgb` 的 `min_samples_leaf=40`，但仍以 §6 的調參／確認切分為準。
- 這份設計沒有處理「模型證明贏不了市場」的情境：若 P4 做完 `info_over_market` 仍不顯著，P5 的第二層（用市場線當輸入）才是出路；這會改變 P4 的價值判斷，不是 P4 能解決的。

## 11. 需要使用者同意的事

P4 本身都在本機與唯讀 DB 內，**不需要**使用者同意即可寫設計、寫計畫、寫程式、跑唯讀評估。需要同意的是：(1) push／合併 main；(2) 把 `FEATURE_SET` 升版並讓排程重訓（改變正式模型的行為）；(3) 任何寫正式 DB（例如日後若要把 `v_game_quality` 建成檢視表）。
