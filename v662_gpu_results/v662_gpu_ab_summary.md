# V6.6.2：RapidOCR CPU / CUDA ONNX Runtime 安全 A/B

**狀態：CUDA_EP_NOT_AVAILABLE**

- 只針對已驗證 SHA-256 的七頁合成 PDF；不修改正式專案、模型或安裝環境。
- 2×+3× OCR、文字 PII、人臉遮蔽與 V5 安全重建均保留。
- ORT 執行緒未調整；僅測 CPU 與 CUDA 執行提供者。
- GPU 工作確認採用 ORT 暫存 profiling 中的 provider kernel 計數，而非只檢查 CUDA 是否列在可用清單。
- 每組均執行完整遮蔽與安全稽核；相同配對逐頁比較 PDF 像素。
- 本工具不儲存 OCR 原文、個資、影像、PDF、還原 map 或原始 ORT profile。
- 本測試可能因 CUDA DLL／版本不相容而失敗；不會自動安裝任何套件。

| 組次 | CPU 秒數 | CUDA 秒數 | CPU kernel events | CUDA kernel events | PDF 像素一致 |
|---|---:|---:|---:|---:|---|

- CPU 中位數：— 秒
- CUDA 中位數：— 秒
- 加速幅度：—%
- PROMISING_AB_ONLY 只代表固定合成文件有加速訊號，並不代表能部署於正式系統。
- 固定 GT 遮蔽通過不代表對所有文件零漏判；GUI 響應未測。
- 若程式被強制結束，請檢查 %TEMP% 內 pii_v662_ 前綴目錄並清除。
