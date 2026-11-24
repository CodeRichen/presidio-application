# V6.6.4：獨立 ONNX Runtime GPU 環境驗證

## 目的與既知條件

V6.6.3：Windows Python 3.12.3、RTX 4060 Laptop 8 GB、NVIDIA 610.62；正式 `.venv` 有 CPU `onnxruntime 1.31.1`、`torch 2.14.0+cpu`、`rapidocr 3.10.0`，沒有 CUDA Execution Provider。

本包只確認**另外建立的** `.venv_ocr_gpu` 中，ONNX Runtime 1.31.1 是否能執行真正的 CUDA kernel，且與 CPU 輸出近似一致。所有 ONNX 模型與輸入值由工具生成，不會讀取、傳送或保存 PDF／OCR／個資／私有 map。**不是整套畢業專題 GPU OCR A/B。**

官方資料：
- ONNX Runtime CUDA EP requirements: https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html
- ORT 1.31.1 Python 3.12 Windows wheel: https://pypi.org/project/onnxruntime-gpu/1.31.1/
- NVIDIA CUDA 13 driver >= 580: https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html

## 執行（PowerShell，位於 presidio_app_allen 專案根目錄）

先用正式的 `.venv` 做不需要 GPU 的工具自測：

```powershell
.\.venv\Scripts\python.exe .\PII_V664_GPU_Isolated\test_v664.py
```

確認自己有磁碟空間並可從官方 PyPI 下載套件後，執行獨立環境建立及安裝（只影響 `.venv_ocr_gpu`，**不修改** `.venv`）：

```powershell
powershell -ExecutionPolicy Bypass -File .\PII_V664_GPU_Isolated\setup_v664_gpu_env.ps1
```

它會使用 `.venv` 的 Python 3.12 建新 venv，僅在新環境安裝 `onnxruntime-gpu[cuda,cudnn]==1.31.1`、`onnx==1.19.1`、`numpy==2.4.6`。可能下載超過 1 GB；若安裝失敗不會修正正式 `.venv`。不需要先安裝完整 CUDA Toolkit 或更換 NVIDIA 驅動。

然後執行合成 GPU 運算驗證：

```powershell
.\.venv_ocr_gpu\Scripts\python.exe .\PII_V664_GPU_Isolated\v664_gpu_smoke.py `
  --results-dir .\v664_gpu_smoke_results
```

在 `v664_gpu_smoke_results` 產生：
- `v664_gpu_smoke_summary.json`
- `v664_gpu_smoke_summary.md`

請上傳以上兩個報告即可。不要上傳整個 `.venv`、任何實際文件、ONNX profile、模型或私有 map。

## 判讀

- `GPU_KERNEL_VERIFIED_SYNTHETIC_ONLY`: 合成 ONNX Conv/Relu 模型實際有 CUDA provider kernel，CPU/GPU 輸出數值近似相同；**不代表 RapidOCR 已切換到 GPU**。
- `CUDA_EP_NOT_AVAILABLE`: 新環境沒列出 CUDA Provider，確認使用的是 `.venv_ocr_gpu` 的 Python。
- `NO_CONFIRMED_CUDA_KERNEL`: Provider 存在但沒有 CUDA Node 的證據；不能聲稱 GPU 生效。
- `CUDA_SESSION_FELL_BACK_TO_CPU`: 建立 Session 時已降回 CPU。
- `OUTPUT_MISMATCH`: GPU/CPU 數值差異超過測試容忍範圍。
- `RUNTIME_ERROR`: 通常是 CUDA/cuDNN DLL 或依賴載入等錯誤；報告只儲存錯誤種類，不保存完整 traceback。

這個獨立環境**只安裝 GPU 核心驗證所需套件**，沒有完整 GUI、Presidio、OCR 的依賴。因此通過後，才規劃完整隔離的應用依賴環境以及原七頁合成 PDF 的 V6.6.2 GPU A/B。正式環境保持 CPU，不進行任何覆蓋或升級。

請把 `.venv_ocr_gpu/` 加入 `.gitignore`，不要提交虛擬環境或大型 CUDA DLL 到 Git。若日後不需要，關閉相關程序後刪除 `.venv_ocr_gpu` 即可。
