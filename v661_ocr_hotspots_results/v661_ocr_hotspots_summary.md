# V6.6.1：OCR 流程細部效能診斷（僅合成 PDF）

**狀態：PROFILE_OK_SINGLE**

- 保留 2×+3× OCR、原本 PII / 人臉偵測和 V5 安全輸出；正式程式未修改。
- 只使用固定 SHA-256 七頁合成 PDF；中間 PDF 和私有 map 正常結束時會刪除。
- 各階段採暫時性計時包裝；量測有額外開銷，**不是加速 A/B**。
- `pii_boxes` 包含 `code_detect`，不可重複加總。
- `ocr_backend` 包含 OCR 引擎呼叫及其轉換；不等同純模型推論時間。
- `other_collect` 是 OCR 收集函式中其餘時間（座標轉換／IP 補抓等）。
- 不能從 27/27 和 17/17 推論真實文件沒有漏判。

| 指標 | 數值 |
|---|---:|
| 測試次數 | 1 |
| 七頁 PDF 純處理中位數 | 181.869 秒 |
| 2×+3× OCR 收集合計 | 174.254 秒 |
| V5 安全輸出 | 5.443 秒 |
| 其他處理估計 | 2.172 秒 |

## OCR 細分結果（所有頁面合計）

| 階段 | 2× 中位數 (秒) | 3× 中位數 (秒) | 收集時間占比 |
|---|---:|---:|---:|
| PDF 畫面渲染 | 1.0262 | 0.8191 | 1.06% |
| Pixmap 轉 PIL | 0.0391 | 0.0835 | 0.07% |
| OCR 影像前處理 | 0.5548 | 1.1909 | 1.0% |
| OCR 引擎辨識 | 60.2597 | 77.2499 | 78.91% |
| PII 偵測與座標框 | 30.2469 | 2.6828 | 18.9% |
| 收集函式其餘處理 | 0.032 | 0.0685 | 0.06% |

- 收集函式中占比最高的項目：`ocr_backend`。

## 各輪狀態

| 輪次 | 處理秒數 | 2× 頁數 | 3× 頁數 | 27/27 | 17/17 | PDF 結構 |
|---|---:|---:|---:|---|---|---|
| 1 | 181.869 | 7 | 7 | 27/27 | 17/17 | True |

## OCR 呼叫的 cProfile 熱點（僅抽樣，非純推論計時）

- 每頁抽樣間隔：2
- 實際 OCR 呼叫：{'2x': 7, '3x': 7}
- 取樣 OCR 呼叫：{'2x': 4, '3x': 4}

| 模組檔名 | 函式 | 次數 | self 秒 | cumulative 秒 |
|---|---|---:|---:|---:|
| `onnxruntime_inference_collection.py` | `run` | 35 | 39.295858 | 127.435364 |
| `main.py` | `__call__` | 8 | 33.492686 | 72.244218 |
| `utils.py` | `normalize` | 8 | 1.115943 | 1.249682 |
| `onnxruntime_inference_collection.py` | `_create_inference_session` | 3 | 0.808389 | 0.83016 |
| `<builtin>` | `<method 'astype' of 'numpy.ndarray' objects>` | 652 | 0.327179 | 0.327179 |
| `<builtin>` | `<built-in method _io.open_code>` | 184 | 0.158158 | 0.158158 |
| `<builtin>` | `<built-in method builtins.isinstance>` | 506121 | 0.154677 | 0.266993 |
| `<builtin>` | `<built-in method _imp.exec_dynamic>` | 6 | 0.137097 | 0.139543 |
| `<builtin>` | `<method 'reduce' of 'numpy.ufunc' objects>` | 10018 | 0.118976 | 0.118976 |
| `main.py` | `order_points` | 972 | 0.105226 | 0.23426 |
| `<builtin>` | `<method 'argmax' of 'numpy.ndarray' objects>` | 36 | 0.093247 | 0.093247 |
| `<builtin>` | `<resize>` | 300 | 0.086 | 0.086 |
| `<builtin>` | `<built-in method nt.stat>` | 923 | 0.077114 | 0.077583 |
| `<builtin>` | `<built-in method numpy.array>` | 6722 | 0.076642 | 0.205267 |
| `<builtin>` | `<method 'encode' of 'ImagingEncoder' objects>` | 1592 | 0.062813 | 0.062813 |

注意：cProfile 有執行開銷；函式累積時間互相包含，不能相加。OCR 原生函式不一定能拆成偵測與辨識兩階段。

## 安全與效能限制

- 此工具不改動正式 GUI、模型、快取、辨識次數及輸出政策。
- 報告不記錄 OCR 文字、函式參數、裁圖、私人還原表或文字雜湊。
- 逐頁時間只與合成 PDF 相關，不含 PII 原文。
- 若強制終止，需檢查 `%TEMP%` 內 `pii_v661_` 前綴的殘留暫存資料夾。
- 下一步需針對占比最高的階段提出 A/B，通過安全回歸才可能部署。
