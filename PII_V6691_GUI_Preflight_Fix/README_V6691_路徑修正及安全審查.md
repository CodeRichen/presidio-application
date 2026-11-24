# V6.6.9.1：正式 GUI 原始碼掛接點修正（唯讀）

## 已確定的檢查器問題

最新 `core.py` 不是直接從 `dispatch_files()` 呼叫 `handler_media.mask_file()`；它是
`dispatch_files()` → `_mask_media()` → `handler_media.mask_pdf`／`mask_image`。
舊版 V6.6.9 只支援直接呼叫，因此 `core_media_entry_call=False` 是**靜態掃描器誤判**。
V6.6.9.1 加入此間接呼叫結構的辨識，並檢查 helper 是否真的引用兩種遮蔽函式且執行它。

## 仍需安全審查的正式程式行為

- `dispatch_files()` 中媒體處理失敗後 `except: ... continue`：整批執行結果可能只回傳部分成功的檔案；不能將空清單視為安全成功。
- `_mask_media()` 對指定 `out` 路徑，例外情況有 `shutil.copy2(path, out)`；資料夾掃描失敗時可能存在未遮蔽複本，必須明確區分失敗／安全結果。
- `scan_folder()` 起始 `shutil.copytree(...)`，因此輸出資料夾本來就可能保留未經遮蔽的原始文件。整個資料夾**不可因掃描結束而被認定完全安全**。
- 本工具不修改以上行為。正式 GPU 整合前應改為隔離測試的 fail-closed 規則、逐檔安全狀態與禁止失敗原稿被當成可安全輸出。

## 執行方法（專案根目錄 PowerShell）

將 `PII_V6691_GUI_Preflight_Fix` 放在 `presidio_app_allen` 根目錄。

```powershell
.\.venv\Scripts\python.exe .\PII_V6691_GUI_Preflight_Fix\test_v6691.py
.\.venv\Scripts\python.exe .\PII_V6691_GUI_Preflight_Fix\v6691_preflight.py --check-only
.\.venv\Scripts\python.exe .\PII_V6691_GUI_Preflight_Fix\v6691_preflight.py `
  --results-dir .\v6691_source_preflight_results
```

上傳 `v6691_source_preflight_results/v669_source_preflight.json` 與
`v6691_source_preflight_results/v669_source_preflight.md`。

**請勿將這個修正版誤認為正式 GPU GUI 整合：它僅解析 Python AST，不啟動正式 GUI、OCR 或 PDF，也不修改程式碼。**
只有固定七頁合成資料已有安全驗收；不要以真實個資文件測試 GPU。
