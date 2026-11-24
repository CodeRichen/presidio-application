# V6.5.1：深入分析 `handler_code.detect()` 的效能瓶頸

## 為何做這個實驗？

上一次 V6.5 的七頁合成 PDF 測試：`media_detect_matches()` 累積約 35.01 秒，其中 `handler_code.detect()` 約 31.89 秒／278 次；`presidio_matches()` 約 3.10 秒。既有頁面快取命中 776 / 1054 次。因為時間是巢狀計算，不能加總當作完整流程時間。`handler_code.detect()` 內部到底是正規表示式、模型、文字預處理還是其他邏輯耗時，**目前仍未知**，不可盲目修改。

本工具以 Python `cProfile` 抽樣量測 `handler_code.detect()` 的內部函式，提供 **自身時間**（self time）與 **累積時間**（cumulative time）。抽樣只在獨立 Python 子程序生效；不修改任何 `.py` 正式檔案，也不修改 PII 辨識規則、模型、2×+3× OCR、V5 PDF 安全輸出或還原 map。

## 安裝

將 `PII_V651_Code_Hotspots` 資料夾完整解壓到 `presidio_app_allen` 專案根目錄。需要：

- 既有專案 `.venv`，可在 Windows 執行完整遮蔽流程。
- 之前的 `PII_V5_Security_Acceptance` 資料夾（**必須包含** `v5_acceptance.py`、`run_7page_pdf_test.py`、`expected_text_and_faces.json` 等檔）。
- 七頁合成測試資料 `PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf`，工具驗證其固定 SHA-256。

```powershell
# 在 presidio_app_allen 專案根目錄中執行
.\.venv\Scripts\python.exe .\PII_V651_Code_Hotspots\test_v651.py

.\.venv\Scripts\python.exe .\PII_V651_Code_Hotspots\v651_benchmark.py --check-only

.\.venv\Scripts\python.exe .\PII_V651_Code_Hotspots\v651_benchmark.py --results-dir .\v651_hotspots_results
```

預設 **每四次抽樣一次**，最多分析 80 次 `handler_code.detect()` 呼叫；其他呼叫仍正常執行且回傳不變。這能讓抽樣分散到不同 PDF 頁面，降低持續量測的額外開銷。

輸出：

```text
v651_hotspots_results/
├── v651_code_hotspots_summary.json
└── v651_code_hotspots_summary.md
```

只上傳這兩份報告即可。**請勿上傳** `media_deid_map.json`、任何 `_restore_map`、原始真實文件、未遮蔽圖片或偵測文字。程式會將合成 PDF 及含裁圖的暫存輸出置於 OS 暫存目錄，正常結束會清除。若遭強制關閉，請檢查臨時目錄。

## 結果如何判讀？

- `PROFILE_OK`：完成抽樣，且 27/27 已標註文字 PII、17/17 已記錄人臉框和 PDF 安全結構檢查通過。這**不是**加速成功。
- `SECURITY_GATE_FAILED`：固定合成文件輸出安全性不符合既有驗收。
- `NO_PROFILE_SAMPLES`：沒有真正量測到 `handler_code.detect()`，不能進一步判定瓶頸。
- `RUN_FAILED`：測試失敗、程序非零退出或逾時。

注意 `top_self_time` 與 `top_cumulative_time` 的區別。比如慢的函式 A 呼叫 B，A 的累積時間包含 B，**兩者不可相加**。輸出中的完整執行秒數含有 cProfile 分析開銷，不可直接和過去 200 秒作加速 A/B。

## 隱私與安全注意

- 工具只允許固定 SHA-256 的合成 PDF。
- 子程序 stdout/stderr 不寫到持久日誌；持久報告只包含函式名稱（無路徑）、呼叫次數、時間、函式所在檔案名稱與行號。
- 不收集 OCR 文字、個資值、輸入參數、文字雜湊、圖片或還原 map。
- **合成資料專用的 `PII_MEDIA_FP_PROFILE=synthetic_face_test` 絕不可套用到真實文件或正常 GUI。**
- 此工具尚未在你的 Windows 模型環境實際執行 OCR；單元測試只驗證分析功能與安全設計。

## 下一階段

看完熱點報告後再選擇小範圍修改。每次候選優化都必須重新執行無分析器的 A/B 測速及七頁像素比較、27/27、17/17、PDF 結構驗收；如涉及新文件與一般使用情境，需要額外人工及獨立 Ground Truth 測試。
