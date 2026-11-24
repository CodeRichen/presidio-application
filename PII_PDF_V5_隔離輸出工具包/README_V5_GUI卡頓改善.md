# V5：PDF 安全輸出獨立程序化（GUI 卡頓改善）

本工具包適用於 `presidio_app_allen` 的現有 PDF 安全輸出架構。**不覆蓋 `ui.py`、`handler_media.py`、OCR、NER、YuNet。** 不修改 PII 辨識策略。可相容已套用 V3 分段監控的 `pdf_secure_output.py`。

## 為什麼這樣做？

V2 三次測試的最長 UI 排程延遲約 2.7–3.1 秒，都與 `pdf.secure_export` 重疊。V3 指出 `pdf.save()` 單次約 3.0–3.6 秒，V4 A/B 測試確認關閉 `deflate` 雖快，但輸出 PDF 增大約五倍。

V5 保留 `garbage=4, deflate=True, clean=True, incremental=False`，把 **PNG 編碼、PDF 建立、壓縮儲存** 移到**獨立 Python 子程序**；GUI 原本的背景執行緒只負責從 **已完成遮蔽** 的 PDF 頁面渲染 RGB 像素，透過 OS 匿名管線送給子程序。**不會將原始 PDF、PDF 附件、未遮蔽頁面、文字層或還原對照表交給子程序**。此點以呼叫者確實已 `page.apply_redactions()` 為前提。

子程序儲存新 PDF 到目標資料夾的專用暫存路徑；**主程序執行既有 `_verify_secure_raster_pdf` 驗證，通過後才 `os.replace()`**。子程序異常、驗證失敗或逾時會保留既有輸出，盡量刪除暫存檔。此結構驗證不代表 OCR 不會漏遮。

## 檔案內容

- `pdf_secure_v5.py`：主程序渲染已遮蔽的 RGB 頁面並傳送給子程序，保留原本驗證和原子取代。
- `pdf_secure_v5_worker.py`：子程序 PNG 編碼、建立全新 PDF、`fresh.save()`，不讀取來源 PDF。
- `install_v5.py`：只在現有 `pdf_secure_output.py` 結尾加入啟用開關，會建立 `bak_v5_*` 備份。
- `test_v5.py`：六項獨立合成測試（像素一致、結構安全、失敗不覆蓋、開關切換等）。
- `compare_v5_gui.py`：比較 V2 前後三次 GUI 延遲與 Worker 耗時。

## 1. 安裝（PowerShell，在專案根目錄執行）

先**完全關閉 GUI**，將 ZIP 解壓於 `presidio_app_allen`，保留 `PII_PDF_V5_隔離輸出工具包` 目錄。

```powershell
.\.venv\Scripts\python.exe .\PII_PDF_V5_隔離輸出工具包\install_v5.py --check
.\.venv\Scripts\python.exe .\PII_PDF_V5_隔離輸出工具包\install_v5.py --apply
.\.venv\Scripts\python.exe -m py_compile .\pdf_secure_output.py
.\.venv\Scripts\python.exe .\PII_PDF_V5_隔離輸出工具包\test_v5.py
```

預期 `Ran 6 tests ... OK`。若 `--check` 不通過，**不要手動覆蓋 `pdf_secure_output.py`**，請上傳最新版程式進行相容性修補。若同名 `pdf_secure_v5*.py` 已存在不同內容，修補器會拒絕覆蓋。

## 2. 使用 V2 測試器測試 V5 的 GUI 反應

以下只使用**先前的合成測試資料**，不適用於一般真實文件的誤判策略。

```powershell
$env:PII_PDF_EXPORT_V5="1"
$env:PII_PDF_EXPORT_V3="0"
.\.venv\Scripts\python.exe .\PII_GUI_V2_Diagnostics\gui_v2_launcher.py --synthetic-test --results-dir .\gui_v5_results
```

在開啟的 UI「檔案」分頁，放入與 V2 **完全相同**的 `7page_pdf_preview.jpg`、`pii_face_7page_scan_source.pdf`，同一 GUI 程序內連續執行 3 次。每次都檢查輸出是否正常。這個測試比較 UI 排程延遲，**不會自動驗證個資完全遮蔽**；仍需另外使用 7 頁 Ground Truth 稽核。

V5 使用子程序，**不保證 PDF 總處理時間縮短**。其主要目的為避免原本單一 Python 程序中的 PNG 編碼及 `pdf.save()` 壓縮阻塞 GUI 排程。剩餘 CPU 與 I/O 競爭仍可能造成短暫延遲。

## 3. 比較 V2 原始結果與 V5

舊 V2 測試檔案應位於 `gui_v2_results`，若原本目錄名稱不同，請改為實際路徑。

```powershell
.\.venv\Scripts\python.exe .\PII_PDF_V5_隔離輸出工具包\compare_v5_gui.py --before-dir .\gui_v2_results --after-dir .\gui_v5_results
```

產生 `gui_v5_results/gui_v5_comparison.json`。請提供該檔與本次 `gui_v5_results/v2_*.json`，評估最高延遲是否由約 3,000 ms 降至建議的 500 ms 以下。

## 4. 停用／移除

**立即停用（無須解除安裝）：** 關閉 UI，再執行：

```powershell
Remove-Item Env:PII_PDF_EXPORT_V5 -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe .\ui.py
```

正式移除 V5 啟動分流（保留 V3 及你後續的其他修改）：

```powershell
.\.venv\Scripts\python.exe .\PII_PDF_V5_隔離輸出工具包\install_v5.py --remove
```

`--remove` 只移除 V5 程式碼標記，不自動刪除兩個 helper，避免誤刪其他檔案。原始 `pdf_secure_output.py` 安裝時已備份。若你打包成 EXE，V5 會拒絕使用其子程序模式（`sys.executable` 不是 Python 直譯器），請維持 V5 關閉，另行設計 EXE 版子程序封裝。

## 5. 安全／效能驗收

- 正常 JPG + 7 頁 PDF 批次處理完成，輸出數量不減。
- 27/27 文字 PII Ground Truth 覆蓋門檻不退步；17 個既有偵測人臉區域仍應遮蔽；另做目視漏臉檢查。
- PDF 頁數、圖片像素、附件數 0、文字層 0、註解 0、連結 0、表單 0，保持原本安全輸出要求。
- 子程序失敗時不能取代既有輸出；不儲存原始 PII 到診斷檔。
- UI 最大排程延遲努力達到 <500ms；p95 應維持低延遲；Worker 總耗時不可明顯惡化。

**重要限制：** V5 無法保證未被 OCR / NER / YuNet 偵測到的敏感資料安全。僅測試「已完成遮蔽的像素 → 新建 PDF」的輸出安全結構。若 Windows 因 CPU 滿載、即時防毒掃描或磁碟因素造成 UI 卡頓，子程序化也可能無法完全消除。
