# V6.6.7.1：混合 OCR 子程序失敗階段診斷

## 為什麼需要這一版？

V6.6.7 的報告顯示 CPU 七頁 PDF 正常完成，但是 hybrid 在 31 秒左右回傳 `CHILD_FAILED`，而且 `preflight_pairs=0`。既有程式把錯誤詳情隱藏，因此**不能判定是 CUDA、GPU worker、程序通訊，還是 OCR adapter 出問題**；`pixel_identical=false` 只是候選 PDF 沒有產生，並不代表兩份實際 PDF 像素不同。

這一版**不執行完整七頁 PDF 遮蔽**，只依序執行：

1. **GPU 模型檢查**：在 `.venv_ocr_gpu` 裡直接重建 V667 worker 使用的 det CUDA、cls/rec CPU RapidOCR；用自行產生的合成字串影像做一次推論，檢查 ONNX Runtime GPU node 紀錄。
2. **跨程序與 Adapter 預檢**：在原本 `.venv` 中讀取**唯一允許的 SHA256 合成 PDF**第 1 頁 2×／3×；用原專案 `handler_media.ocr_words` 與**原始 V667 GPU worker**各做一次 OCR，僅在記憶體比較輸出格式、文字、字框及行編號。若 worker 建立、收發或比較失敗，報告只寫安全的固定狀態碼和異常類型（不寫異常內容）。

沒有新模型參數，也不更動正式專案、`.venv`、`.venv_ocr_gpu`、V667 原始工具包。

## 安裝條件

將本 ZIP 解壓在 `presidio_app_allen` 專案根目錄。請保留之前的 `PII_V667_Full_PDF_Hybrid_AB/`、兩個 Python 虛擬環境和 `PII_Face_PDF_7page_Test/`。

僅允許原固定測試 PDF：SHA-256 `df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49`。**禁止拿真實文件執行。**

## Windows PowerShell 指令

```powershell
# 1. 只測試工具，不初始化模型
.\.venv\Scripts\python.exe .\PII_V6671_Hybrid_Diagnostics\test_v6671.py

# 2. 只檢查路徑與固定合成 PDF SHA-256
.\.venv\Scripts\python.exe .\PII_V6671_Hybrid_Diagnostics\v6671_diagnose.py --check-only

# 3. 執行 GPU 初始化、跨程序通訊、兩組 OCR adapter 預檢
.\.venv\Scripts\python.exe .\PII_V6671_Hybrid_Diagnostics\v6671_diagnose.py `
  --results-dir .\v6671_diagnostics_results
```

若環境正常，這個診斷通常遠短於完整七頁 PDF 的 CPU 對照測試；它是定位工具，**不能拿執行時間來宣稱加速**。

## 結果與處理方法

| 狀態 | 下一步 |
|---|---|
| `GPU_WORKER_INITIALIZATION_NEEDS_DIAGNOSIS` | 查看 `gpu_probe.stage` 和 `error_type`，找出 ORT DLL、RapidOCR 初始化或 GPU kernel 階段 |
| `WORKER_OR_ADAPTER_EXCEPTION` | 查看 `adapter_probe.stage`，區分 `APP_IMPORT`、`WORKER_START`、`CPU_OCR_1`、`GPU_OCR_1` 等 |
| `OCR_ADAPTER_MISMATCH` | 查看 `mismatch_kind`；請提供 `ocr_backend.py`、`handler_media.py` 的最新版本（**不要含私人 OCR 結果**）以對齊 adapter |
| `PROVIDER_ROUTE_NOT_VERIFIED` | 檢查 det CUDA、cls/rec CPU 執行紀錄；不得採用 |
| `WORKER_AND_ADAPTER_PREFLIGHT_OK` | 工作程序和預檢通過，原 V667 問題可能在後續階段；再進行有階段紀錄的完整 PDF 測試 |

**只需上傳**：

```text
v6671_diagnostics_results/
├── v6671_hybrid_diagnosis_summary.json
└── v6671_hybrid_diagnosis_summary.md
```

只保存固定狀態、異常類型、計數和耗時。PDF、OCR 原文、圖片、字框、私人還原表、traceback、原始 ORT profile 不寫入報告。臨時 profiling 由 `TemporaryDirectory` 正常清除。若強制終止，請自行檢查 `%TEMP%` 中 `pii_v6671_` 前綴暫存資料夾。

**安全保證的限制**：這不是完整 PII 安全驗收，也未測 GUI；即使全部通過，也只是表示 GPU worker 與目前 OCR adapter 預檢正常，不能據此部署。
