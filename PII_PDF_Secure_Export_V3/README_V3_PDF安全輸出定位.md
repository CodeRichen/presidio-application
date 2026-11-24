# PDF 安全輸出 V3：分段瓶頸定位

目的：針對 V2 三次測試中 **GUI 最長約 2.7～3.1 秒卡頓均與 `pdf.secure_export` 重疊**的現象，將原有的安全重建過程分成更小的計時階段。**V3 先定位瓶頸，不改變渲染解析度、PNG 編碼方式、原子寫入、安全結構驗證，亦不進行可能增加個資外洩的效能捷徑。**

## 1. 注意版本相容與備份

此修補器依照先前提供的 `pdf_secure_output.py` 內容設計；你本機版本若改過，**只會停止，不會硬套**。不要將測試包內的 `_reference_pdf_secure_output.py` 覆蓋到專案主程式！那只是單元測試固定樣本。

先關閉所有 GUI，將 `PII_PDF_Secure_Export_V3` 整個資料夾解壓縮到 `presidio_app_allen` 根目錄；**不要**用 ZIP 中任何舊版核心程式取代目前使用中的 `ui.py`、`handler_media.py`。

```powershell
# 以下均在 presidio_app_allen 專案根目錄執行
.\.venv\Scripts\python.exe .\PII_PDF_Secure_Export_V3\test_export_v3.py
.\.venv\Scripts\python.exe .\PII_PDF_Secure_Export_V3\apply_export_v3.py --file .\pdf_secure_output.py --check
```

如果檢查通過，再套用：

```powershell
.\.venv\Scripts\python.exe .\PII_PDF_Secure_Export_V3\apply_export_v3.py --file .\pdf_secure_output.py --apply
.\.venv\Scripts\python.exe -m py_compile .\pdf_secure_output.py
```

套用工具會建立 `pdf_secure_output.py.before_v3.bak` 作為自動備份、`pdf_secure_output.py.v3_patch_state.json` 來確認能否安全還原，並將 `pdf_export_probe_v3.py` 放到主程式旁。探針**預設關閉**。

如果 `--check` 顯示不相容，請停止，不要手動猜測插入位置；應提供目前最新的 `pdf_secure_output.py` 以供適配。

## 2. 以既有 V2 Launcher + V3 計時共同監測

使用**合成資料**測試（勿使用含真實個資的正式文件）。先清掉舊報告或將舊目錄重新命名，以免混入過去數據。新開 PowerShell 啟動：

```powershell
$env:PII_PDF_EXPORT_V3="1"
$env:PII_PDF_EXPORT_V3_DIR="gui_v3_export_results"
.\.venv\Scripts\python.exe .\PII_GUI_V2_Diagnostics\gui_v2_launcher.py --synthetic-test
```

在 GUI 檔案分頁，使用和上次相同的 `7page_pdf_preview.jpg` + `pii_face_7page_scan_source.pdf`，連續測試三次。每次成功後都會寫出 `gui_v3_export_results/pdf_v3_*.json`，每一筆只含階段耗時、頁碼、時間戳記；**不含檔名、路徑、頁面內容、OCR 值、原始裁切或對照表**。

同一輪執行的 V2 報告仍輸出至 `gui_v2_results/`。如需交叉分析哪一段阻塞 GUI，請保留並提供新產生的 V2 `v2_*.json` 與 V3 `pdf_v3_*.json`。

關閉 GUI 後：

```powershell
.\.venv\Scripts\python.exe .\PII_PDF_Secure_Export_V3\summarize_export_v3.py --results-dir .\gui_v3_export_results
.\.venv\Scripts\python.exe .\PII_GUI_V2_Diagnostics\summarize_v2.py --results-dir .\gui_v2_results
```

請回傳 `pdf_v3_summary.md`、`pdf_v3_summary.json`、3 份 `pdf_v3_*.json`，和對應的新 V2 診斷報告。

## 3. 新增的分段計時

| 事件 | 對應原有運算 |
|---|---|
| `page.render` | `source_page.get_pixmap(dpi=200, ...)` |
| `page.encode_png` | `pix.tobytes("png")` |
| `page.new_page` | `fresh.new_page(...)` |
| `page.insert_image` | `target.insert_image(..., stream=encoded_png)` |
| `pdf.save` | `fresh.save(..., garbage=4, deflate=True, clean=True, incremental=False)` |
| `pdf.verify` | `_verify_secure_raster_pdf(...)` |
| `pdf.atomic_replace` | `os.replace(temporary_path, output_path)` |

每一筆除了 `duration_ms`，還有 `start_epoch_ms` / `end_epoch_ms`，方便對照 V2 的 UI 延遲時間；因 V2 的開始時間精度較低，**對時需容忍約 1 秒誤差**。時間重疊只是線索，不等於證明 GIL 問題。

## 4. 驗收與安全檢查

- 每輪 JPG / PDF 都要正確產生，無發生 OCR/PII 遮蔽退步。
- PDF 必須仍保持：預期頁數、附件 0、文字層 0、註解 0、連結 0、每頁一個影像，並以你現有的 Ground Truth/視覺檢查確認 PII 和人臉已遮蔽。
- 如 `pdf_v3_*.json` 沒出現，先確認已**重新啟動 GUI**與 `PII_PDF_EXPORT_V3=1`；若 `--check` 不相容，切勿硬套。
- 不上傳 `media_deid_map.json` 或 `PRIVATE_restore_map_DO_NOT_SHARE.json` 等含原始裁切的對照資料。

## 5. 停用與還原

如果只是停止量測，不需修改程式：

```powershell
Remove-Item Env:PII_PDF_EXPORT_V3 -ErrorAction SilentlyContinue
Remove-Item Env:PII_PDF_EXPORT_V3_DIR -ErrorAction SilentlyContinue
```

若要恢復安裝 V3 前的程式版本，**先關閉 GUI**：

```powershell
.\.venv\Scripts\python.exe .\PII_PDF_Secure_Export_V3\apply_export_v3.py --file .\pdf_secure_output.py --restore
```

若你在套用 V3 後又自行修改了 `pdf_secure_output.py`，還原器會因雜湊不同而拒絕覆蓋；請人工比較備份後合併變更，不要強制還原。

## 6. 本機自我測試的意義

已在可用的 Python / PyMuPDF 環境對測試版本執行 6 項檢查，包含修補器安全防呆、備份/還原、修補前後合成 PDF 頁面像素相同、沒有繼承附件/文字層/連結，以及報告不記錄個資。**這並不代表已在你的 Windows + Presidio + RapidOCR + YuNet 真實流程驗證；實際效能仍須執行上述 GUI 回歸。**
