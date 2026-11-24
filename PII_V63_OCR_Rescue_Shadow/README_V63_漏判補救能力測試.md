# V6.3 — 2× 漏判、3× 補救能力測試（實際 OCR，影子模式）

## 目的與現階段證據

V6.2 在 8 頁困難案例 + 7 頁既有合成 PDF 上，標註的 57 個 PII 均已被 2× OCR 找到；無法藉此驗證「3× 真正救回 2× 漏判時，選擇性策略是否能正確保留 3×」。V6.3 專門測試此情境。**不是正式加速修補程式，不更動 handler_media.py、GUI、模型、PDF 輸出或對照表。**

## 測試方法

1. 以 Pillow 畫出新的純影像合成 PDF。每頁都有清楚可辨的虛構 PII（anchor）和一個刻意增加辨識難度的虛構 PII（probe）。在繪圖階段計算每個 PII 的 Ground Truth 座標，與 OCR 偵測流程獨立。
2. 採用小字、小型 email、低對比、局部模糊、JPEG 壓縮、背景線條、混合大小字及清晰對照等八種生成配置；重複配置、不同種子擴增。**找到 2× 漏判但 3× 補救並不保證**，以 Windows RapidOCR/NER 真正輸出決定。
3. 每頁實際執行 `handler_media._collect_pdf_ocr_boxes(page, 2.0)` 與 `(..., 3.0)`，使用原本 `_merge_pdf_multizoom_boxes` 合併。影子版使用與 V6.2 完全相同的 `decide(Features(...))` 規則，**決策僅使用 2× 特徵**，不能使用 3× 或 GT 作弊。
4. 以 GT 的 **同類別座標框 ≥95% 覆蓋率**判定是否有命中。這與 V6.2 評估方法一致，但只屬於幾何代理指標，**不是 PDF 黑框安全驗證**。
5. 分成 discovery（探索）與 holdout（不同亂數種子保留測試）。保留測試仍是相同生成器的版型家族，並非真實世界獨立分布。報告記錄已標註區域的補救次數、真正危險的跳過、雙解析度本身的漏判及推估耗時；不存 OCR 原文或值。

## 安裝

將 ZIP 解壓縮到 `presidio_app_allen` 專案根目錄，保留 `PII_V63_OCR_Rescue_Shadow` 資料夾。Windows PowerShell 在專案根目錄執行；沿用你已通過 V6.2 的 `.venv`，不需重新訓練模型。

```powershell
# 1. 工具測試：不載入 OCR 模型
.\.venv\Scripts\python.exe .\PII_V63_OCR_Rescue_Shadow\test_v63.py

# 2. 檢查合成 fixture PDF、GT 和無文字層
.\.venv\Scripts\python.exe .\PII_V63_OCR_Rescue_Shadow\v63_experiment.py --check-only

# 3. 正式實驗：探索 16 頁、不同種子留出 8 頁
.\.venv\Scripts\python.exe .\PII_V63_OCR_Rescue_Shadow\v63_experiment.py --results-dir .\v63_ocr_results
```

首次只想檢查執行環境時，可以先執行比較短的診斷（仍須花時間跑 OCR）：

```powershell
.\.venv\Scripts\python.exe .\PII_V63_OCR_Rescue_Shadow\v63_experiment.py --train-count 4 --holdout-count 2 --results-dir .\v63_ocr_smoke
```

預設每組最多 2400 秒，必要時可帶 `--timeout 3600`。可用 `--seed 12345` 產生另一批合成資料。**不要將真實文件傳給這套實驗工具**；它只接受自行生成的合成 PDF，並在 TemporaryDirectory 中清理。

## 報告

成功執行後：

```text
v63_ocr_results/
  v63_rescue_summary.json
  v63_rescue_summary.md
```

- `rescues_in_probes`：特製困難 PII 框，2× 沒有充分覆蓋而雙解析度成功；這是主要實驗目標。
- `dangerous_skips`：上述補救需要 3×，但 V6.2 條件式策略卻判斷跳過 3×。**只要一個就判定 UNSAFE_SKIP_FOUND**。
- `dual_gt_gaps`：兩種解析度合併後仍沒充分覆蓋的已標註區域。雙解析度本身有漏判時，不可把實驗視為安全。
- `selective_3x_saved_s_estimate`：若略過對應 3× 呼叫，理論上省下的時間。**不是實際加速**。
- `gt_evaluation`：各 GT 只輸出角色、類別及 pass/fail，不輸出文字、來源 PDF、原始 OCR 字詞、私人 map 或影像。

## 結果解讀（嚴格防止錯誤成功結論）

| 狀態 | 含義 / 行動 |
|---|---|
| `UNSAFE_SKIP_FOUND` | 策略在已證明 3× 補救的 PII 仍跳過 3× → **拒絕部署** |
| `BASELINE_GROUND_TRUTH_GAPS` | 雙解析度也漏掉某些已知 PII → 先處理模型或 GT 問題 |
| `NO_CONFIRMED_3X_RESCUES` | 尚未找到符合條件的救援樣本 → 不能驗證選擇性策略安全 |
| `DISCOVERY_RESCUES_ONLY` | 只有探索組有救援樣本，尚缺 holdout 救援驗證 |
| `RESCUES_OBSERVED_SHADOW_ONLY` | 探索與留出皆找到救援、未見危險跳過，**仍不能推論真實資料安全** |
| `INCOMPLETE` | 執行失敗/逾時，不能得出結論 |

> 注意：程式實際一律執行 2× 和 3×，並未依據決策省略 OCR。V6.3 的目的，是**找到反例或證據缺口**，不是產出能自動證明沒有漏判的安全門檻。

## 分享結果

請只上傳 `v63_rescue_summary.json` 和 `v63_rescue_summary.md`。不要上傳暫存的未遮蔽 PDF、O​​CR 原文、私有還原對照表、含真實個資的 log。若 V6.3 仍無法找到真實補救案例，下一步應優先研究 **保持 2×+3× 不變的 OCR backend 加速**，而不是調整門檻以讓更多頁面跳過 3×。
