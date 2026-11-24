# V6.6.2：RapidOCR ONNX Runtime GPU 安全加速 A/B（僅合成文件）

## 實驗背景

V6.6.1 的七頁合成 PDF，純處理約 181.869 秒；OCR backend 約 137.51 秒，約占 OCR 收集階段 78.91%。抽樣 `cProfile` 顯示 ONNX Runtime 是主要熱點，但不等於已確定 CUDA 可以加速，也不能直接把 cProfile 的累積時間當純推論時間。

V6.4 已對 **CPU 執行緒**做過測試，本版測的則是**相同 OCR 模型使用 明確指定的 CPU Execution Provider 或 CUDA Execution Provider**。不減少 2×/3× OCR，不改中文/英文 PII 偵測、人臉處理、PDF 安全輸出、模型檔案、正式程式，也不會自動安裝套件。

## 重要安全限制

- **只允許指定 SHA-256 的七頁合成 PDF**：`PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf`。請勿用真實資料測試。
- **不能在正式 GUI 啟用這個試驗補丁**。所有 ORT provider 變更僅在測試子程序存在。
- 必須同時通過 27/27 文字 PII、17/17 已記錄人臉框、PDF 結構安全，以及七頁逐像素相同，才會評估加速。
- GPU `get_available_providers()` 顯示 CUDA **不代表**模型確實在 GPU 執行；程式使用 ORT 暫存 profiling 的執行提供者事件計數驗證 CUDA kernel。看不到 GPU kernel 一律標記不可採用。
- ORT profiling 同時作用於 CPU、CUDA 實驗組，可能有額外時間開銷，所有數值仍屬實驗；不能和 V6.6.1 的無 A/B 診斷直接比較。
- ORT profile、遮蔽後 PDF、私人還原 map 與其他中間檔僅留在系統暫存目錄，正常結束自動刪除。不要強制結束；被迫中止時檢查 `%TEMP%` 裡 `pii_v662_` 開頭資料夾。
- 報告只保存秒數、執行提供者的 kernel **數量**和驗收結果，不保存 OCR 原文、模型輸出、影像或還原 map。

## 使用方式（Windows PowerShell）

把 `PII_V662_ORT_GPU_AB` 解壓在 `presidio_app_allen` 根目錄；保留已經使用過的 `PII_V5_Security_Acceptance`、`PII_Face_PDF_7page_Test`。關閉正在執行的 GUI。

### 1. 工具測試

```powershell
.\.venv\Scripts\python.exe .\PII_V662_ORT_GPU_AB\test_v662.py
```

### 2. 先查目前 ORT 是否提供 CUDA

```powershell
.\.venv\Scripts\python.exe .\PII_V662_ORT_GPU_AB\v662_benchmark.py --check-only
```

預期可看到：`FIXTURE_OK`、`ORT_IMPORT_OK`、`CUDA_EP_AVAILABLE`、`CPU_EP_AVAILABLE`。如果出現 `CUDA_EP_AVAILABLE: False`，**請先不要執行 A/B**，工具會產生 `CUDA_EP_NOT_AVAILABLE` 報告，不會嘗試安裝或修改套件。

要額外檢查版本，可在專題 `.venv` 執行：

```powershell
.\.venv\Scripts\python.exe -c "import onnxruntime as ort; print(ort.__version__); print(ort.get_available_providers())"
.\.venv\Scripts\python.exe -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
```

注意：PyTorch 有 CUDA 不等於 onnxruntime 有 CUDA。**不要直接在目前正式 `.venv` 裡盲目升級 `onnxruntime-gpu`**，先檢查 ONNX Runtime、CUDA、cuDNN 版本對應，建議在複製的虛擬環境評估相容性，避免影響已經通過安全驗收的系統。

官方說明：
- <https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html>
- <https://onnxruntime.ai/docs/install/>

### 3. 確認 CUDA 可用後執行小規模測試

```powershell
.\.venv\Scripts\python.exe .\PII_V662_ORT_GPU_AB\v662_benchmark.py `
  --repeats 1 `
  --results-dir .\v662_gpu_smoke
```

### 4. 正式 A/B（先兩組）

```powershell
.\.venv\Scripts\python.exe .\PII_V662_ORT_GPU_AB\v662_benchmark.py `
  --repeats 2 `
  --results-dir .\v662_gpu_results
```

強制 CPU 與 CUDA 交錯執行（AB / BA），每組以獨立子程序避免永久配置更動。每個子程序都完整處理一次七頁合成 PDF；兩組共執行四次。

## 報告

```text
v662_gpu_results/
├── v662_gpu_ab_summary.json
└── v662_gpu_ab_summary.md
```

| 狀態 | 意義 |
|---|---|
| `CUDA_EP_NOT_AVAILABLE` | 專題環境沒有提供 CUDA EP，未執行測試 |
| `CUDA_EXECUTION_UNVERIFIED` | CUDA Session / GPU kernel 未得到實際證據，禁止宣稱 GPU 加速 |
| `OCR_SESSION_NOT_INTERCEPTED` | 未偵測到 RapidOCR 的 ORT Session 建立；候選未生效 |
| `CHILD_FAILED` | 子程序失敗，可能因 CUDA/cuDNN 版本不相容，報告不保存敏感除錯文字 |
| `SECURITY_GATE_FAILED` | 27/27、17/17 或 PDF 結構驗收失敗 |
| `PIXEL_DIFFERENCE` | GPU 與 CPU 遮蔽頁面像素不同，即使更快也不得採用 |
| `NO_CONFIRMED_GAIN` | 效能改善低於 10% 或退步 |
| `PROMISING_AB_ONLY` | 兩組測試達到至少 10% 加速、GPU 執行已驗證且安全像素相同；**仍非正式部署證明** |

## 可能的限制

有些 ONNX Runtime / RapidOCR 版本可能不是透過可攔截的 `onnxruntime.InferenceSession` 物件建立 Session，工具將回報未生效，不能當加速成功。如果 `CUDAExecutionProvider` 列為可用但 DLL 載入失敗，也不能默默把 CPU fallback 視作 GPU。GPU 和 CPU 可能產生不同的文字框、誤判或座標，必須以實際 PDF 安全驗收與像素等價檢查決定是否值得研究。

**這是實驗工具，不是正式程式修補。** 即使測到加速，也需要擴充旋轉、低對比、多頁掃描等案例並測 GUI 響應，才可評估是否部署。
