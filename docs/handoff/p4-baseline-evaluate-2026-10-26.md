# P4 改前基線：`python -m jobs.evaluate`（12 季，唯讀）

來源：主線 2026-10-26 訊息轉述使用者執行結果（原始輸出未存檔；部分數字另見 `p4-ablation-*.json` 的 `baseline_selfcheck`）。
資料：feature table 15,036 場，eligible 13,915，有盤口的 final 14,607；report 段 n=12,736（tune 段 n=274，即最早一季的前 274 場）。

margin（report MAE／bias／winner-acc／ATS 命中／ROI@min_edge 0.04）：
- const 11.834／−0.455／56.4%／49.9%／−4.8%
- elo 10.708／−0.469／65.2%／49.9%／−4.8%
- ridge 10.642／−0.314／65.6%／49.9%／−4.8%（n_graded 6,265）
- hgb 10.756／−0.332／65.0%／49.4%／−5.7%
- blend 10.663／−0.323／65.3%／49.3%／−5.9%

cover（report，n=12,502）：ridge brier 0.26007、logloss 0.7149、cal_slope −0.027、cal_intercept −0.006；市場基準 brier_market 0.24995；elo brier 0.26162；hgb 0.26292；blend 0.26074。edge_sweep（0.02～0.06）ridge ROI 全在 −4.4%～−4.7%，沒有任何 edge 門檻轉正。resid_sigma ridge 13.60。

可由 `p4-ablation-*.json` 的 `baseline_selfcheck` 逐位數重現的：ridge report MAE 10.642229932112338、cover Brier 0.26006605366396995（n=12,736／12,502）。其餘數字（Elo、hgb、blend、ATS、ROI）目前只有這份轉述為來源。
