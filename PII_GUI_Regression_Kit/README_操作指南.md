# Presidio PII 專題 — GUI 效能與穩定性回歸測試

此工具配合現有 `ui.py`、`core.py`、`handler_media.py`。**不覆蓋/修改原本程式碼**。Windows + 專案 `.venv` 執行。執行時應關閉其他已啟動的 UI，避免同時更動對照表或輸出檔。

## 0. 安裝

請將整個 `PII_GUI_Regression_Kit` 資料夾放到 `presidio_app_allen` 專案根目錄。

```powershell
# 於 presidio_app_allen 執行
.\.venv\Scripts\python.exe -m pip show psutil pillow pymupdf
.\.venv\Scripts\python.exe .\PII_GUI_Regression_Kit\test_regression_tools.py
```

你的專案原本就使用 `psutil` / `Pillow` / `PyMuPDF`；若缺少套件，應先以既有依賴版本補裝，避免不必要升級。

## 1. 合成測試資料：圖片、PDF、混合批次

```powershell
.\.venv\Scripts\python.exe .\PII_GUI_Regression_Kit\gui_regression_launcher.py --synthetic-test
```

**僅限這批合成資料**使用 `--synthetic-test`；會設定 `PII_MEDIA_FP_PROFILE=synthetic_face_test` 和 `PII_MEDIA_FP_REPORT=1`，模擬先前 v2 減少標題誤遮蔽的模式。正式文件不能使用此模式。

在開啟的 UI「檔案」頁：

1. 選擇 `samples/pii_faces/pii_face_01_front.png`，按「開始處理」一次，測單張 PNG。
2. 若已準備 `samples/faces/face_07_mask.jpg`，測單張 JPG。若沒有就使用其他已知人臉 JPG。
3. 測 `PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf`，測 7 頁掃描 PDF。
4. 選一張 PNG、一張 JPG、上述 PDF，組成混合批次，**連續測 3 次**。每次核對「已產生的去識別化檔案」清單是否正確。
5. 完成後關閉 UI。再重新啟動程序測一次，記錄冷啟動與重複執行的差別（模型快取可能影響結果）。

每次「開始處理」會在 `gui_regression_results/run_*.json` 留一份**不含 OCR 原文與還原 map**的紀錄：

- `click_to_ui_s`：按鍵啟動工作到 UI 收到完成結果的時間。
- `worker_s`：由背景執行緒執行核心工作的時間。
- `wait_and_ui_s`：其餘排隊與 UI 回報時間（包含鎖等待）。
- `ui_lag_max_ms / ui_lag_p95_ms`：Tk `after()` 心跳的延遲，100ms 間隔；是**UI 響應能力代理指標**，不是使用者按鍵直接延遲。
- `rss_peak_mb`：整個 Python 程序的記憶體峰值。不是獨立模型或單頁 PDF 的用量。
- `status / exception_type / output_count`：成功、失敗、輸出數。**OK 僅表示沒有捕捉到例外；請另外做隱私安全稽核。**

**注意**：使用 UI 處理文件可能會更新專案原本的 `media_deid_map.json` 或其他對照表，請只使用合成測試資料；不將原始私有還原 map 放進測試報告。輸出的檔案預設路徑仍由你既有的 `handler_media.py` 決定，可能會覆蓋同名舊結果，重要舊檔請先備份。

## 2. 錯誤恢復測試（非破壞性）

單獨重新啟動：

```powershell
.\.venv\Scripts\python.exe .\PII_GUI_Regression_Kit\gui_regression_launcher.py --synthetic-test --simulate-error-once
```

- 加入一個合成測試檔，**第一次**按開始處理：程式會在進入核心流程前故意丟出 `RuntimeError`，不處理任何檔案；預期 UI 顯示「處理失敗」，不當掉，按鈕恢復可用。
- **第二次**按開始處理：同一個 UI 應該能正常重新處理；記錄為 `OK`。
- 檢查第一次不能有新輸出；第二次應能看到輸出。若錯誤後 UI 無法恢復，此項失敗。
- 這只測 **worker 例外與 UI 恢復**；不涵蓋「OCR/face 模型中途失效」導致的部分輸出安全性，需要另外進行故障注入測試。

## 3. PDF 與圖片輸出結構檢查

將路徑改成 UI 實際輸出的檔案：

```powershell
.\.venv\Scripts\python.exe .\PII_GUI_Regression_Kit\verify_gui_outputs.py `
  "C:\\path\\to\\pii_face_7page_scan_source_masked.pdf" --expect-pages 7 `
  --json-out .\gui_regression_results\output_structure_audit.json
```

檢查頁數、附件、文字層、註解、連結與 EOF；圖片則檢查是否可解碼。

這只是 **PDF 結構 smoke test**，**不代表所有文字 PII 和人臉都遮蔽成功**。請保留之前的 `pdf_face_7page_audit.json` 和目視逐頁檢查，尤其是 27 個文字 PII、17 個 face-map 區域。

## 4. 彙整結果

```powershell
.\.venv\Scripts\python.exe .\PII_GUI_Regression_Kit\summarize_gui_regression.py `
  --results-dir .\gui_regression_results
```

產出：`gui_regression_results/gui_timing_summary.md` 及 `gui_timing_summary.json`。

若有之前的測試紀錄，請先把 `gui_regression_results` 改名保存，以免把不同版本的測試混合計算。

## 5. 建議驗收準則（建議值，可依實際需求調整）

| 項目 | 驗收重點 |
|---|---|
| 功能與穩定性 | 已知 7 頁合成 PDF、多張混合批次至少連續成功 3 次 |
| UI 使用者體驗 | 主視窗仍能切分頁/捲動，心跳延遲應盡量低，連續超過 500ms 建議調查 |
| 錯誤恢復 | 模擬錯誤後可再按開始處理、無 UI 崩潰 |
| 安全性 | 27/27 文字 PII、face-map 17/17 預期覆蓋與 PDF 結構測試仍通過 |
| 記憶體 | 連續執行的 RSS 使用應能趨於穩定；常駐模型/快取可能使峰值暫時上升，不可只憑一次判定洩漏 |
| 不完整輸出 | 模型例外、無效 PDF、磁碟失敗的中途錯誤應另做 fail-closed 驗證 |

## 6. 將哪些檔案回傳分析

- `gui_timing_summary.md`
- `gui_timing_summary.json`
- `output_structure_audit.json`
- 若有失敗，再提供隱去個資的終端錯誤摘要，不要上傳 `PRIVATE_restore_map_DO_NOT_SHARE.json` 或 `media_deid_map.json`。

## 已知限制

- 本工具無法在沒有 Windows GUI、完整離線 OCR/NER/YuNet 模型的環境下直接代替你的使用者操作。
- 心跳測試會受到系統排程、電腦負載影響；`ui_lag_max_ms` 不是嚴格的 GUI 卡死證明。
- 不會自動辨識臉部漏判、敏感文字未遮蔽；需搭配人工與 Ground Truth 驗證。
- 不會新增自動重試或修改原本的失敗安全性政策。
