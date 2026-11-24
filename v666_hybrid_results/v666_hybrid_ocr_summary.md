# V6.6.6：RapidOCR CPU／全 CUDA／混合 GPU 偵測 A/B

- 狀態：**HYBRID_OCR_EQUIVALENT_SYNTHETIC_ONLY**
- 僅固定 SHA-256 七頁合成 PDF，完整 2×+3× OCR。
- 混合模式：det 使用 CUDA；cls、rec 強制 CPU，逐模型檢查 Provider Node events。
- 透過隔離程序攔截 InferenceSession 達成實驗路由，非 RapidOCR 官方獨立開關。
- 只比較 OCR 文字、字框與分數，不執行完整 PII／人臉／PDF 安全回歸。
- 不更改正式 .venv 或專案檔案；不保存原文、裁圖、字框、私有 map、profile 或影像。

| OCR 模式 | 累積秒數（不含預熱） | 相較 CPU |
|---|---:|---:|
| cpu | 102.347 | —% |
| cuda | 74.93 | 26.79% |
| hybrid | 43.353 | 57.64% |

| 與 CPU 比對 | OCR 配對 | 等價 | 文字一致 |
|---|---:|---:|---:|
| cuda | 14 | 14 | 14 |
| hybrid | 14 | 14 | 14 |

- OCR 分數為 ±0.002 容許值，文字為完全相同；框座標容許 0.5 輸入像素。
- CUDA 子模型可能有部分 CPU fallback；只強制 cls／rec 完全 CPU。
- 未比較正式 GUI、冷啟動或 PDF 遮蔽結果；本版不能用於部署。
- 強制終止時，需檢查 %TEMP% 中 pii_v666_ 開頭的暫存目錄。
