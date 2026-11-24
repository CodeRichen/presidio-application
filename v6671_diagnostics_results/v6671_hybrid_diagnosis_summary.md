# V6.6.7.1：混合 OCR 失敗階段診斷（僅合成測試）

- 狀態：**GPU_WORKER_INITIALIZATION_NEEDS_DIAGNOSIS**

| 檢查 | 結果 | 階段 | 例外類型 |
|---|---|---|---|
| 獨立 GPU 引擎 | INCOMPLETE_MODEL_SESSIONS | RAPIDOCR_CONSTRUCT | — |
| 原始 OCR Adapter 預檢 | NOT_RUN | — | — |

- 預檢通過：0 / 0。
- 不同類型：無紀錄。
- 不執行完整 PDF 脫敏，不比較 PDF 像素，也不計算加速百分比。
- 原始 V6.6.7、正式程式、兩個虛擬環境均未修改。
- 僅使用固定 SHA-256 合成 PDF；不記錄 OCR 文字、框、影像、map、異常訊息或 traceback。
- 只有 GPU 和 Adapter 兩項都通過，才考慮重新進行完整 PDF 安全 A/B。
