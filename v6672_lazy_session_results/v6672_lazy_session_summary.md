# V6.6.7.2：RapidOCR 延遲 Session 初始化安全診斷

- 狀態：**OCR_ADAPTER_MISMATCH**
- 這是診斷工具，非完整 PDF 速度／遮蔽安全 A/B。
- CUDA 偵測證據需來自實際 Node events，而不是 Provider 清單。
- 原版程式、正式虛擬環境與舊工具不更動；使用獨立實驗 worker。

| 階段 | 結果 | 位置 | 例外類型 |
|---|---|---|---|
| GPU 合成推論 | GPU_DET_CUDA_VERIFIED_SYNTHETIC_ONLY | ORT_PROFILING | — |
| 固定第 1 頁 OCR 預檢 | OCR_ADAPTER_MISMATCH | COMPARE_1 | — |

- 建立 RapidOCR 後觀測 Session：{'det': 0, 'cls': 0, 'rec': 0}
- 合成 OCR 後觀測 Session：{'det': 1, 'cls': 1, 'rec': 1}
- GPU det CUDA Node：380
- OCR adapter 完全一致：0 / 1
- 差異類型：WORD_COUNT_DIFFERENCE

- adapter 要求文字、字框、line_id 等欄位精確一致；若不一致即停止。
- 沒有寫出 OCR 內容、圖片、字框、原文摘要、個資、PDF、原始 profiler 或還原表。
- 目前固定合成文件的結果不代表真實文件的辨識安全性。
