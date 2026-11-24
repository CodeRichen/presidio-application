# V6：OCR 效能 A/B 對照實驗（僅合成資料）

**目的**：量測目前七頁 PDF 的雙解析度 OCR（zoom=2.0 + 3.0）實際成本，並安全評估只執行 2.0 的漏遮風險。此工具**不修改** `ui.py`、`handler_media.py`、`pdf_secure_output.py`，也**不改變正常啟動 GUI 的行為**。

## 使用前

- 先完成 V5 GUI / PDF 安全回歸驗收；測試環境必須維持原本 RapidOCR、NER、YuNet 設定。
- 將此資料夾解壓在 `presidio_app_allen` 專案根目錄，並保留前一步使用的 `PII_V5_Security_Acceptance` 資料夾。
- 必須保留固定七頁合成 PDF：`PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf`。程式會驗證 SHA-256；**不允許用真實文件**。
- 執行期間關閉 GUI；只用合成資料，避免更動真實還原對照表。原始輸出與私有 map 只存於 TemporaryDirectory，完成後自動清除。標準輸出/標準錯誤被丟棄，不保存任何可能包含敏感值的模型日誌。

## 執行（PowerShell，專案根目錄）

```powershell
.\.venv\Scripts\python.exe .\PII_V6_OCR_Experiment\test_v6.py
.\.venv\Scripts\python.exe .\PII_V6_OCR_Experiment\v6_experiment.py --check-only
.\.venv\Scripts\python.exe .\PII_V6_OCR_Experiment\v6_experiment.py --results-dir .\v6_ocr_experiment_results
```

實驗依序執行兩個獨立 Python 子程序，**每一種模式都重新載入模型**，因此總秒數含初始化成本，不完全等於 OCR 自身成本：

1. **dual_zoom**：原本的 zoom2 + zoom3 結果合併（無改動）
2. **zoom2_only**：只在此實驗進程中攔截 zoom3 呼叫並返回空結果，不改正式檔案，也不發布其遮蔽 PDF

第二組仍使用完整 PII/人臉與 PDF 安全輸出，且自動檢查 27 個已知文字區域、17 個**記錄到**的人臉框和 PDF 結構；並計算「原本黑色像素、候選版不再黑色」的像素數（**不保存像素**）。

## 輸出

- `v6_ocr_experiment_results/v6_ocr_ab_summary.json`
- `v6_ocr_experiment_results/v6_ocr_ab_summary.md`

`PASS_SYNTHETIC_ONLY` **絕不代表可以移除真實文件的 zoom3**。即使七頁合成資料 27/27 PASS，也可能在其他字型、低對比、側掃、較小字或特殊 PII 上漏遮。若觀察到候選缺失，先保留雙解析度；再研究不會減少 OCR 覆蓋的加速路線，例如後端初始化與批次化、相同文字辨識結果重用、引擎執行效能調整。

## 工具不做的事

- 不自動套用任何候選到正式程式。
- 不比較未經標註的實際文件或聲稱真實世界 recall。
- 不上傳原始 PII、還原對照表、PDF、頁面影像、文字、bounding box 坐標或模型輸出。
- 不保證 GUI 最長延遲進一步下降；V5 已解決原先 3 秒卡頓，這次目標是整批總處理時間。

將摘要 `.json` 和 `.md` 上傳以進行下一階段判斷。
