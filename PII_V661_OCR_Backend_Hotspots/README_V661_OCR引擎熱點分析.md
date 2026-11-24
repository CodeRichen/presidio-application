# V6.6.1 — OCR 引擎內部熱點分析（安全取樣版）

## 為什麼執行 V6.6.1

V6.6 在固定七頁合成 PDF 的單次診斷中，純處理 180.844 秒；其中 `ocr_backend` 累計 136.435 秒（2×：59.653 秒；3×：76.783 秒），約占 OCR 收集階段 78.58%。這個欄位包含引擎與結果轉換，不等於純模型推論。

V6.6.1 對既有 `handler_media.ocr_words` 呼叫做暫時性 `cProfile` 函式抽樣，找出 OCR 引擎呼叫鏈中的主要 Python 函式和可見的原生函式。**這是定位瓶頸，並非新加速方式**。

## 必要條件

- Windows，沿用目前能正常辨識的 `.venv`，以及現有的 RapidOCR、中文 NER、YuNet 等模型。
- 必須在 `presidio_app_allen` 專案根目錄，解壓出 `PII_V661_OCR_Backend_Hotspots` 資料夾。
- 必須保留並通過既有 V5 安全驗收包 `PII_V5_Security_Acceptance`，以及固定的 `PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf`。
- **請先關閉 GUI**，勿使用真實文件；工具核對固定合成測試 PDF 的 SHA-256。
- 不修改 `handler_media.py`、`ui.py`、`ocr_backend.py`、模型、PII 或 PDF 安全輸出設定。

## 執行指令（PowerShell）

```powershell
# 1. 測試工具（本機單元測試）
.\.venv\Scripts\python.exe .\PII_V661_OCR_Backend_Hotspots\test_v661.py

# 2. 只檢查安裝狀態與固定合成 PDF
.\.venv\Scripts\python.exe .\PII_V661_OCR_Backend_Hotspots\v661_benchmark.py --check-only

# 3. 執行一次完整 PDF 的 OCR 熱點分析
.\.venv\Scripts\python.exe .\PII_V661_OCR_Backend_Hotspots\v661_benchmark.py `
  --repeats 1 `
  --results-dir .\v661_ocr_hotspots_results
```

第一次先跑一次即可。需要驗證時間的穩定性再使用 `--repeats 2` 與**另一個**結果資料夾。本工具預設在七頁中隔頁抽樣，因此對每組 2× / 3× 分別取樣 4 次 OCR 呼叫；**所有頁面依然會完整執行兩種解析度 OCR**。

## 產生的檔案

```text
v661_ocr_hotspots_results/
├── v661_ocr_hotspots_summary.json
└── v661_ocr_hotspots_summary.md
```

重點檢視 JSON 的 `summary.backend_hotspots.top_self_time`（函式**自身**耗時）及 `top_cumulative_time`（包含子呼叫，彼此重疊不可加總），並留意 `calls_by_zoom`、`profiled_calls_by_zoom` 和 `unique_static_functions`。時間因取樣與 cProfile 開銷，**不得直接與 V6.6 未加相同 profiler 的 180.844 秒做 A/B 比較**。

- 如果看到 ONNX Runtime、`InferenceSession.run` 等：原生模型推論耗時可能較高。但 Python profiler 通常看不到 ONNX 模型的 detector / recognizer 內部工作分配，需要後續針對實際 RapidOCR 版本測試。
- 如果 OpenCV、NumPy、影像變換函式靠前：優先評估 OCR 引擎內部前後處理，不要直接削減 2× 或 3×。
- 如果只有上層呼叫且分不出細項：報告如實標示分析深度有限，下一步需依使用者實際的 `ocr_backend.py` 設計更細的專用計時，不應猜測瓶頸。

## 安全與限制

- 只處理固定、驗證過 SHA-256 的**合成 PDF**；每次保持 2× + 3×、既有 PII 與人臉辨識、V5 安全輸出。
- 每輪仍須完成 **27/27 已標註文字區域、17/17 已記錄人臉框與 PDF 結構**自動驗收；`--repeats 2` 另外比較輸出 PDF 逐頁像素一致性。
- 報告僅儲存數字、Python 函式識別名稱與**模組檔案 basename**；不儲存 OCR 內容、函式參數、原始路徑、模型輸入、還原 map、原 PDF 或裁圖，也不儲存 `.prof` / 原始 pstats。
- 暫存遮蔽 PDF 和私人還原 map 在正常退出時會刪除；強制中止時請檢查 `%TEMP%` 下 `pii_v661_` 開頭的資料夾。
- 既有合成 GT 通過不能證明真實文件零漏判；需要更多文件、實際 GUI 與更廣泛安全回歸後才有部署討論。

完成後上傳 `v661_ocr_hotspots_summary.json` 和 `.md`，再決定下一個真正的 OCR 加速 A/B 實驗。
