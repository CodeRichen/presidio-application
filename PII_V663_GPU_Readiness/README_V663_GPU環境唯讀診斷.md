# V6.6.3：OCR GPU 環境唯讀診斷

## 為什麼進行此步驟？

V6.6.2 報告顯示 `CUDA_EP_NOT_AVAILABLE`，ONNX Runtime 可以正常匯入，但 `CUDAExecutionProvider` 沒出現在可用 Provider 清單，因此 **CPU／CUDA 加速 A/B 根本未執行**，不能宣稱 GPU 加速成功或失敗。

接下來要先辨別正式 `.venv` 中的 ONNX Runtime 安裝版本、PyTorch CUDA/cuDNN 版本、GPU 驅動，**不要在正式 `.venv` 中嘗試安裝或覆蓋 GPU 套件**。

## Windows PowerShell 操作

在 `presidio_app_allen` 專案根目錄，把本資料夾解壓成 `PII_V663_GPU_Readiness`。

1. 測試診斷工具（可在 Windows 離線執行）：

```powershell
.\.venv\Scripts\python.exe .\PII_V663_GPU_Readiness\test_v663.py
```

2. 執行唯讀環境盤點：

```powershell
.\.venv\Scripts\python.exe .\PII_V663_GPU_Readiness\v663_gpu_readiness.py `
  --results-dir .\v663_gpu_readiness_results
```

3. 上傳這兩份檔案供分析：

```text
v663_gpu_readiness_results/
├── v663_gpu_readiness_summary.json
└── v663_gpu_readiness_summary.md
```

## 收集內容和隱私限制

只讀取 Python、ONNX Runtime、PyTorch、RapidOCR 等套件版本，列出 ORT 的 Providers，確認 PyTorch CUDA/cuDNN 與 `nvidia-smi` 的 GPU 名稱、驅動版本與顯示記憶體。**不讀取 PDF、OCR 原文、模型輸出、圖片、個資、token、模型路徑、使用者目錄、GPU 序號或 UUID**。不執行 OCR 模型，也不聯網、安裝或更新套件。

若不希望寫檔，可使用：

```powershell
.\.venv\Scripts\python.exe .\PII_V663_GPU_Readiness\v663_gpu_readiness.py --print-only
```

`CUDA_EP_LISTED_NOT_EXECUTION_PROOF` 只表示 Provider 列在清單，不等於 CUDA kernel 執行成功。是否能真正使用 GPU，仍須在**獨立測試環境**實際建立 ONNX Runtime 模型 Session，確認 CUDA kernel，最後再執行 V6.6.2 固定合成 PDF 的安全與像素 A/B 測試。

## 為何不能直接安裝最新版？

ONNX Runtime GPU 套件的 CUDA／cuDNN 相依性依版本不同。部分新版本的預設 PyPI wheel 已經轉向 CUDA 13.x；目前機器的驅動與 PyTorch 可能仍是 CUDA 12.x。未確認版本前不應選擇特定安裝指令。請先上傳診斷結果，再建立隔離環境進行相容性測試。

官方文件：
- https://onnxruntime.ai/docs/install/
- https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html

## 安全界線

本工具為唯讀環境盤點，**不包含任何加速方案**；正式應用、既有 PDF 安全輸出、雙解析度 OCR、合成測試 GT 都未改動。
