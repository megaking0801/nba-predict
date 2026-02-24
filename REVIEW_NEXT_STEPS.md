# 專案健檢與下一步建議（逐步執行）

## 我已檢查的範圍
- 檔案：
  - `PROJECT_CONTEXT.md`
  - `requirements.txt`
  - `.github/workflows/daily_nba.yml`
  - `auto_nba_predict.py`
  - `jobs/cache_nba.py`
  - `jobs/sync_daily.py`
  - `jobs/train_base_model.py`
  - `jobs/train_calibrator.py`
  - `jobs/settle_daily.py`

## 主要發現（先修這些最有價值）

1. **專案文件尚未完成，交接成本高**
   - `PROJECT_CONTEXT.md` 仍有大量 placeholder（`???`、`...`、空白狀態），目前無法當作可執行 runbook。

2. **依賴未鎖版本，長期會有「今天能跑、下週壞掉」風險**
   - `requirements.txt` 全部是未 pin 版本套件。

3. **Workflow 中 season 與安裝流程有維護風險**
   - `daily_nba.yml` 把 `NBA_SEASON` 寫死為 `2025-26`，季別切換需要手動改檔。
   - CI 安裝套件直接 `pip install ...`，未使用 `requirements.txt`，本機與 CI 可能漂移。

4. **錯誤處理有靜默吞錯，除錯不易**
   - 多處 `except Exception: pass`（例如 `jobs/sync_daily.py`、`jobs/cache_nba.py`），若資料抓取失敗，日誌資訊不足。

5. **主程式檔案過大，後續改動風險高**
   - `auto_nba_predict.py` 超過千行，UI、資料抓取、DB、模型推論混在同檔，功能回歸測試難度高。

---

## 建議的「一步一步」執行順序

### Step 1（今天就做）：先把文件補完整
- 補齊 `PROJECT_CONTEXT.md` 的：
  - `games`、`model_registry`、`nba_cache` 的讀寫欄位與流程。
  - 各 job 的 input/output 與失敗 fallback。
  - 目前上線狀態（What works / Current issue / Next steps）。
- 目標：新同事看文件就能知道如何手動跑一次 pipeline。

### Step 2：固定依賴版本
- 產生一份可重現版本（例如使用 `pip freeze` 或手動 pin 主套件）。
- CI 改為 `pip install -r requirements.txt`。
- 目標：本機/CI/排程環境一致。

### Step 3：改善錯誤觀測能力（先做最少必要）
- 把 `except Exception: pass` 改成至少 `print`/logging（含 team/date/context）。
- 對外部來源（ESPN / nba_api / Odds API）統一 log 失敗原因與重試次數。
- 目標：排程失敗時能在 5 分鐘內定位問題。

### Step 4：把 season 參數化
- 將 workflow 的 `NBA_SEASON` 改為：
  - `workflow_dispatch` 可輸入（已有 run input，可再加 season input），或
  - 用 repo variable/secrets 控制。
- 目標：換季不改碼。

### Step 5：開始拆 `auto_nba_predict.py`
- 建議先拆 3 個模組：
  - `db.py`（連線、建表、upsert）
  - `features.py`（抓資料、特徵工程）
  - `predict.py`（模型載入與機率校正）
- 每次只搬一塊，搬完先 smoke test。
- 目標：降低未來改動風險。

---

## 你接下來可以直接執行的短清單（實務版）

1. 補完 `PROJECT_CONTEXT.md`（30~60 分鐘）。
2. 鎖 `requirements.txt` 版本並更新 workflow 安裝方式（30 分鐘）。
3. 先修 2~3 個最關鍵的 `except ... pass`（30 分鐘）。
4. 新增 workflow 的 `season` 輸入（15 分鐘）。
5. 規劃 `auto_nba_predict.py` 第一次拆檔（先拆 DB 層）。

> 建議每做完一步就手動跑一次：`cache -> sync -> settle`，避免一次改太多。
