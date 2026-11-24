# V6.6.5：RapidOCR 真實 OCR CPU／GPU 篩選（只用固定合成 PDF）

## 已確認的前提

V6.6.4 的獨立 `.venv_ocr_gpu` 已使用 ONNX Runtime 1.31.1 真正執行合成 ONNX CUDA Nodes（6 次），CPU/GPU 最大數值誤差 2.9e-06。**這並未證明 RapidOCR 能使用 GPU，也未證明完整 PII PDF 加速。**

本版只增加 **RapidOCR 3.10.0** 與 **PyMuPDF／Pillow** 至已存在的 `.venv_ocr_gpu`，不改動正式 `.venv`、`handler_media.py`、OCR 模型、PII 規則、V5 PDF 安全重建或 GUI。

官方 RapidOCR 文檔指出 3.10.0 可以用 `EngineConfig.onnxruntime.use_cuda` 切換 CUDA，且 OCR 模型首次呼叫才可能載入；GPU 並不一定更快。已知 `cudnn_conv_algo_search=EXHAUSTIVE` 可能因文字行尺寸變化讓 GPU 顯著變慢，因此**實驗配置**採 `DEFAULT`，保留原模型與識別功能。參考：
- https://rapidai.github.io/RapidOCRDocs/latest/install_usage/rapidocr/usage/
- https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/config.yaml
- https://github.com/docling-project/docling/issues/4167

## Windows PowerShell 指令（在 presidio_app_allen 專案根目錄）

將 ZIP 解壓，應出現 `PII_V665_RapidOCR_GPU_Trial` 資料夾。**請先關閉正式 GUI，不要將測試虛擬環境提交 Git。**

### 1. 工具自測（使用正式 Python，但不載入專題 OCR／PDF）

```powershell
.\.venv\Scripts\python.exe .\PII_V665_RapidOCR_GPU_Trial\test_v665.py
```

### 2. 補齊獨立 GPU OCR 測試套件（只修改 `.venv_ocr_gpu`）

```powershell
powershell -ExecutionPolicy Bypass -File .\PII_V665_RapidOCR_GPU_Trial\setup_v665_rapidocr_gpu.ps1
```

`pip` 可能安裝或下載開源 OCR 模型資源與依賴，但程式不會上傳文件；**如果需要完全斷網，請先預先備妥模型與依賴套件，否則此安裝步驟不能離線完成。** 正式去識別化流程維持原有本機架構。

### 3. 環境確認

```powershell
.\.venv_ocr_gpu\Scripts\python.exe .\PII_V665_RapidOCR_GPU_Trial\v665_ocr_trial.py --check-only
```

### 4. CPU／GPU 14 個 OCR 配對實測（首次只做一輪）

```powershell
.\.venv_ocr_gpu\Scripts\python.exe .\PII_V665_RapidOCR_GPU_Trial\v665_ocr_trial.py `
  --repeats 1 `
  --results-dir .\v665_rapidocr_gpu_results
```

只使用固定 SHA-256 七頁**合成** PDF，所有頁面 2× 與 3× OCR 都會處理。影像轉換仿照目前 `handler_media.py`（灰階、Contrast 1.8、UnsharpMask 1.5/150/2）。在單一隔離程序分別建立 CPU 與 GPU RapidOCR，預熱後交錯比較文字、行框座標與信心分數。原始文字、字框、裁圖、PDF、ONNX profile **不寫入報告**；原生 profile 只放臨時資料夾，正常結束時清理。

GPU 每個 Session 必須真實產生 CUDA Node events；**只看到 CUDA 在 available_providers 清單上不算通過**。CPU 對照 Session 必須真正走 CPU Node events。這是 OCR-only 篩選，**不執行 27/27、17/17、完整 PDF 像素或 V5 安全輸出驗收**；正式部署仍需完整端到端 A/B。

## 請上傳兩份結果

```text
v665_rapidocr_gpu_results/
├── v665_rapidocr_gpu_summary.json
└── v665_rapidocr_gpu_summary.md
```

結果狀態：
- `OCR_GPU_EQUIVALENT_SYNTHETIC_ONLY`：本合成固定 PDF 上，OCR 文字完全一致、行框位置在 0.5 px 內、信心分數偏差在 0.002 內，且存在 CUDA OCR kernel。**僅是先決條件，不是正式安全驗收通過**。
- `OCR_OUTPUT_DIFFERENCE_NEEDS_REVIEW`：辨識文字、行框或分數有差異，不可直接替換引擎。報告只提供差異次數，沒有文字內容。
- `NO_CONFIRMED_CUDA_OCR_KERNEL` / `GPU_SESSION_FELL_BACK_TO_CPU`：GPU 未得到實際使用證據。
- `NO_TEXT_DETECTED_UNSUITABLE_FIXTURE`：本次影像沒有得到可供比較的文字。
- `RUNTIME_ERROR`：套件、模型下載、設定、記憶體等問題；為避免洩漏，只寫入錯誤類型，不存 traceback。可提供安全化的錯誤類型，必要時再針對不含個資的安裝環境診斷。

**注意**：原始 ONNX Runtime profiling 可能含路徑／模型資訊，不能上傳；只上傳 JSON／MD 摘要。若強制終止程式，檢查 `%TEMP%` 前綴 `pii_v665_` 的殘留暫存資料夾。正式專案不改動。

## 下一步

只有在 V6.6.5 CUDA OCR 確認真正執行，且 OCR 層級輸出相容、效能值得繼續時，才研究如何隔離接入**完整七頁遮蔽安全 A/B**。後續必須維持 2×+3×、驗收 27/27 已知 PII、17/17 已記錄人臉框、PDF 結構及逐頁像素等價，並測試 GUI。
