# PII 專題：UI 日誌效能優化（V1）

## 目的與範圍

這份補丁針對 GUI 日誌洪流導致的 Tkinter 排程卡頓做**最小範圍修改**。
它不會變更 `core.dispatch_files()`、`handler_media.py`、YuNet、RapidOCR、NER、PDF 安全輸出或 `media_deid_map.json`。

### 具體修改

1. `QueueWriter`：過量日誌時不讓生產者卡在 GUI 顯示佇列；重要錯誤訊息保留。單筆非常長的 GUI 日誌會截短。
2. `_poll()`：原本 `while True` 會一直處理佇列直到完全清空；改成每輪最多 **250 項**、約 **12ms** 的讀取時間預算。
3. `_append_log_batch()`：將多筆日誌合成一次 `ScrolledText.insert()`，不再每筆 `configure + insert + see + configure`。
4. 記錄視窗最多保留約 **3000 行**，避免長時間使用造成元件歷史內容無限累積。
5. 佇列剩餘訊息多時，每 25ms 再安排下一輪；無待處理訊息時每 100ms 輪詢。

> 注意：這是 **UI 日誌呈現優化**，不會縮短 OCR／NER／PDF 本身的 Worker 推論時間。你上次的最長排程延遲 2.74–3.89 秒有可能還包含模型執行、GIL、磁碟或其他原因；這個補丁**不能保證**消除所有延遲。

## 安裝（Windows PowerShell）

1. **先關閉所有正在執行的 GUI**。
2. 在 `presidio_app_allen` 專案根目錄解壓縮 ZIP，得到 `UI_Log_Optimization_Kit/` 資料夾。
3. 先執行工具本身單元測試：

```powershell
.\.venv\Scripts\python.exe .\UI_Log_Optimization_Kit\test_ui_optimization.py
```

4. 檢查修補程式可否匹配你電腦目前的 `ui.py`：

```powershell
.\.venv\Scripts\python.exe .\UI_Log_Optimization_Kit\apply_ui_optimization.py --file .\ui.py --check
```

5. 確認回報 `[PASS] 可以套用` 後才真正套用：

```powershell
.\.venv\Scripts\python.exe .\UI_Log_Optimization_Kit\apply_ui_optimization.py --file .\ui.py
.\.venv\Scripts\python.exe -m py_compile .\ui.py
```

補丁會先產生 `ui_before_logopt_YYYYMMDD_HHMMSS.py.bak` **完整備份**，再安全替換 `ui.py`。如果發現既有版本的 `QueueWriter` 或 `_poll()` 已有不同修改，會拒絕套用；這時**不要**用 ZIP 內的舊版參考檔覆蓋目前的 `ui.py`，請提供你本機最新 `ui.py` 以便製作精準 patch。

## 回歸測試

使用與上次相同的輸入，以免不同資料量造成不公平比較。

```powershell
.\.venv\Scripts\python.exe .\PII_GUI_Regression_Kit\gui_regression_launcher.py --synthetic-test
```

GUI 檔案分頁選：

- `7page_pdf_preview.jpg`
- `pii_face_7page_scan_source.pdf`

至少執行 3 次。**先將舊的 `gui_regression_results` 複製或改名歸檔**，避免新舊測試混在同一份摘要。測試結束後：

```powershell
.\.venv\Scripts\python.exe .\PII_GUI_Regression_Kit\summarize_gui_regression.py --results-dir .\gui_regression_results
```

重點比較：

| 指標 | 優化前基準 | 目標／觀察方式 |
|---|---:|---|
| `ui_lag_max_ms` | 2736–3892 ms | 希望顯著下降；若仍大於 500ms，進一步定位卡頓階段 |
| `ui_lag_p95_ms` | 約 16–17ms | 希望維持低延遲 |
| `worker_s` | 約 193–209s | 預計相近，不應明顯退步 |
| `rss_peak_mb` | 約 1916–2660MB | 檢查記憶體是否持續升高 |
| 輸入／輸出 | 每次 2／2 | 必須維持成功 |

建議在一次測試中故意觸發 `--simulate-error-once`，確認錯誤訊息有顯示，且第二次仍可完成。

## 驗證 PDF 安全性

補丁未修改核心遮蔽，但仍建議對產生的 PDF 再執行現有安全回歸流程：頁數、附件 0、註解 0、文字層 0、Ground Truth 和 FACE 影像覆蓋率。並且使用肉眼檢查七頁是否有漏遮與誤遮。

## 還原舊版

先關閉 GUI。把修補程式回報的備份檔，例如 `ui_before_logopt_20261119_100000.py.bak` 複製回專案 `ui.py`（先保留新檔以利比較）：

```powershell
Copy-Item .\ui.py .\ui_logopt_failed.py -Force
Copy-Item .\ui_before_logopt_YYYYMMDD_HHMMSS.py.bak .\ui.py -Force
```

上述 `YYYY...` 要替換為你電腦實際產生的備份名稱。

## 隱私提醒

- `ui.py` 原本就會把辨識日誌寫到 GUI；其中可能含姓名、人臉標籤與敏感內容。此補丁不會另外儲存日誌到檔案，也不會傳上網，但記錄視窗本身仍應避免對外展示原始內容。
- 若過載而省略部分**畫面日誌**，並不代表跳過了 PII 偵測或遮蔽；正式測試應以回歸稽核報告為準，不以 GUI 日誌數量代表辨識準確率。
- 此版本的測試在隔離的 Python 模擬環境中完成，**尚未在你的 Windows Tkinter、YuNet、RapidOCR 實際流程驗證效能提升**。
