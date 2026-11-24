# TC-STR OCR Benchmark（可貼入 Canva）

測試資料：TC-STR 7k-word / test；100 張相同圖片；seed=42。

| 指標 | RapidOCR | Windows OCR |
|---|---:|---:|
| Exact Match | 74.00% | 1.00% |
| CER（越低越好） | 24.65% | 93.28% |
| 完整正確張數 | 74/100 | 1/100 |
| 空辨識張數 | 19 | 86 |
| 單張處理時間中位數 | 4.487 秒 | 0.007 秒 |

評估方法：使用專題的 handler_media OCR 流程，按相同圖片與相同字元比對政策計算。
CER 以整批字元編輯距離加總／Ground Truth 字元數加總計算。
報告的處理時間包含影像前處理與 OCR 推論；不包含資料下載或 GUI 操作。
注意：TC-STR 為裁切文字辨識測試，不等於整頁 PDF 的 PII 遮蔽安全性。
