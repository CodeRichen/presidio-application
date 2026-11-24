# V6.6.5 RapidOCR CPU / CUDA OCR-only A/B（固定合成 PDF）

- 狀態：**OCR_GPU_EQUIVALENT_SYNTHETIC_ONLY**
- 只使用 SHA-256 固定的七頁合成 PDF，2×+3× 共 14 個頁面／解析度配對。
- 同一獨立 .venv_ocr_gpu；CPU 及 GPU 皆使用 RapidOCR 3.10.0 與 ORT GPU 1.31.1。
- GPU 使用 CUDA 的 cudnn_conv_algo_search=DEFAULT，避免 EXHAUSTIVE 重複搜尋造成延遲。
- 此測試不是完整 PDF 遮蔽／PII／人臉／輸出安全回歸，正式程式沒有修改。
- 報告不儲存 OCR 文字、個資字串、文字雜湊、字框座標、圖片或原始 ONNX profile。

| 指標 | 結果 |
|---|---:|
| OCR 配對數 | 14 / 14 |
| 輸出等價配對 | 14 |
| 文字完全一致配對 | 14 |
| 字框偏差 ≤0.5 px 配對 | 14 |
| CUDA Node 事件 | 15810 |
| CPU Node 事件 | 18399 |
| CPU OCR 累積處理秒數 | 119.854 |
| GPU OCR 累積處理秒數 | 112.137 |
| 純 OCR 差異 | 6.44% |

本數據不含正式 APP PII／人臉／V5 PDF 輸出，也不代表可部署加速。
如結果存在 OCR 文字、框或信心分數差異，禁止直接替換正式引擎。
首次模型載入不列入比對計時，不能拿本數據當完整 GUI 效能。
若強制中止，請檢查 %TEMP% 的 pii_v665_ 前綴暫存目錄。
