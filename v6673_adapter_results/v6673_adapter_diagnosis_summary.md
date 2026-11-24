# V6.6.7.3：OCR Adapter 數量差異診斷

- 狀態：**DIAGNOSTIC_FAILED**
- 固定 SHA-256 的七頁合成 PDF，只使用第 1 頁的 2×／3× OCR。
- 正式程式與兩套虛擬環境不修改；不執行完整 PII、人臉、PDF 安全回歸。
- 僅保存筆數、行群組數、總字元數與比較布林值，不保存文字、雜湊、裁圖、座標、PDF 或私有 map。
- 「去空白文字相同」**只是診斷線索**，並不表示 OCR Adapter 安全等價。

| 解析度 | 正式版筆數 | 混合版筆數 | 正式版行群組 | 混合版行群組 | 去空白串接相同 | 差異類型 |
|---|---:|---:|---:|---:|---|---|
| 2× | — | — | — | — | — | WORD_COUNT_DIFFERENCE |

- 2× 診斷線索：`INVALID_OCR_ADAPTER_DATA`

- 比對成功：0 / 1
- 模型路由已確認：False
- 失敗位置：COMPARE_2X，例外類型：KeyError
- 即使狀態是 ADAPTER_PREFLIGHT_OK，仍需完整七頁 PDF 的 PII／人臉／結構／像素安全驗收。
