# V6.6.3：OCR GPU 環境唯讀檢查

**狀態：CUDA_EP_MISSING_CPU_DIST_PRESENT**

本工具只讀取套件版本與 GPU Provider，未讀取任何 PDF、OCR 內容或私人對照表，亦未安裝任何套件。

| 項目 | 結果 |
|---|---|
| Python | 3.12.3 (64-bit) |
| 作業系統 | Windows |
| onnxruntime | 1.31.1 |
| onnxruntime-gpu | 未安裝或未知 |
| ORT 匯入 | True |
| CUDA Execution Provider 可列出 | False |
| CPU Execution Provider 可列出 | True |
| PyTorch | 2.14.0+cpu |
| PyTorch CUDA 編譯版本 | 未安裝或未知 |
| PyTorch CUDA 裝置可用 | False |
| PyTorch cuDNN 版本 | 未安裝或未知 |
| nvidia-smi 可用 | True |
| 系統 CUDA_PATH 是否設定 | False |

## GPU／驅動（不含序號與 UUID）

- GPU 1: NVIDIA GeForce RTX 4060 Laptop GPU；驅動 610.62；顯示記憶體 8188 MiB

## 已安裝 GPU 相關套件

- nvidia-cuda-runtime-cu12: 未安裝或未知
- nvidia-cudnn-cu12: 未安裝或未知
- nvidia-cuda-runtime-cu13: 未安裝或未知
- nvidia-cudnn-cu13: 未安裝或未知
- onnxruntime-directml: 未安裝或未知

注意：`CUDAExecutionProvider` 出現在清單中，不等於 OCR 確實執行 CUDA kernel。
請先回傳本報告，不要直接修改正式 `.venv`。
