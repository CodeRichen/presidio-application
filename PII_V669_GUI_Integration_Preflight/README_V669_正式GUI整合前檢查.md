# V6.6.9｜正式 GUI 混合 GPU OCR 整合前的唯讀相容性檢查

## 目的與界線

前一階段 V6.6.8.1 在**隔離 Tkinter**、固定七頁合成 PDF 上測得：CPU 點擊到完成約 206.218 秒；混合模式約 146.694 秒，固定測試資料驗收通過，混合模式心跳最大延遲 247.718 毫秒。這**不是正式 `ui.py` 的量測**。

目前已確認先前版本的典型呼叫鏈：

`App.run_files()` → `App._start()`（背景執行緒）→ `core.dispatch_files()` → `handler_media.mask_file()` → PDF 遮蔽。

**但先前來源可能過時。** 本工具請在你本機的目前專案根目錄執行，以**唯讀 AST 解析**核對當前檔案。它完全不 import 專案模組、不啟動 GUI 或 GPU、不執行 OCR、不處理 PDF、不讀取私人還原 map，不修改正式檔案。

狀態 `STATIC_HOOKS_READY_FOR_REVIEW` 只表示必要掛接點的**名稱與基本呼叫形狀仍存在**；不是安全上線許可，也不是完整語意或執行驗證。

## 安裝位置

將 ZIP 中 `PII_V669_GUI_Integration_Preflight` 資料夾放進專案 `presidio_app_allen` 根目錄，與你真正的 `ui.py`、`core.py`、`handler_media.py`、`ocr_backend.py` 並列。**不要用本工具包取代任何正式程式碼。**

## Windows PowerShell

在 `presidio_app_allen` 根目錄執行。

```powershell
# 1. 不依賴專案模型的單元測試
.\.venv\Scripts\python.exe .\PII_V669_GUI_Integration_Preflight\test_v669.py

# 2. 僅檢查，不輸出報告（如掛接點改變，可能回傳 exit code 1）
.\.venv\Scripts\python.exe .\PII_V669_GUI_Integration_Preflight\v669_preflight.py --check-only

# 3. 輸出完全不包含原始碼／PII 的靜態報告
.\.venv\Scripts\python.exe .\PII_V669_GUI_Integration_Preflight\v669_preflight.py `
  --results-dir .\v669_source_preflight_results
```

**報告**：

- `v669_source_preflight_results/v669_source_preflight.json`
- `v669_source_preflight_results/v669_source_preflight.md`

工具**不覆蓋非空的既有結果資料夾**。如要重跑，換新目錄名稱，例如 `--results-dir .\v669_source_preflight_results_2`。

## 會檢查哪些事情？

1. `ui.py` 是否有 `App.run_files`、`_start`、`_poll`、`threading.Thread`、`core.dispatch_files`、`done` 事件等預期掛接點。
2. `core.py` 是否仍有 `dispatch_files`，是否呼叫 `handler_media.mask_file`。
3. `core.dispatch_files` 內的 `except ... continue` 是否可能**把媒體處理失敗當作略過繼續**。此項為**審查旗標**，不表示現有程式一定不安全，但正式 GPU 模式必須另行確認失敗處理。
4. `handler_media.py` 是否還有 `mask_pdf`、`mask_image`、`mask_file`、`ocr_words` 與 `ocr_words_with_backend`。
5. `ocr_backend.py` 是否會以 `return_word_box=True` 呼叫 OCR，以及重用 `rapid_result_to_words()`。
6. 舊版安全工具、固定合成 PDF 與兩套 Windows 虛擬環境是否存在（只看是否存在，不核對其內容／執行）。

**不做**：實際 CUDA 驗證、模型精度、語意正確性、PDF 逐像素比對、正式 GUI 計時、檔案還原、資料清理完整性驗證。

## 報告狀態

| 狀態 | 含義 |
|---|---|
| `STATIC_HOOKS_READY_FOR_REVIEW` | 必要掛接點的靜態結構符合預期，可以開始人工核對最新版原始碼 |
| `SOURCE_SIGNATURE_CHANGED_REVIEW_REQUIRED` | 有掛接點找不到，必須根據**你目前版本**重新規劃整合 |
| `SOURCE_FILES_MISSING_OR_UNREADABLE` | 缺少原始碼，或有語法／編碼／存取問題；沒有完成檢查 |

即便第一種狀態，**也不要直接啟用 GPU 正式模式**。

## V6.6.9 後半段：正式 GUI 整合的正確順序

1. 上傳**目前實際使用**的 `ui.py`、`core.py`，並搭配上述純統計報告。不要上傳對照表、模型、真實個資文件、private map 或原始 profiling 內容。
2. 在隔離的**程式副本**中製作 GPU 模式的開關；預設維持正式 CPU，GPU 僅能使用固定合成測試 PDF，除非全套安全策略已重新審核。
3. 一併修正實驗模式的**錯誤即停止**及清理。若 GPU 模式失敗、adapter 異常、PDF 安全 Gate 不合格，GUI 必須顯示失敗，不能回傳部分或未驗證的檔案。不可在錯誤後不經清理就靜默改用 CPU。
4. 兩套 Python 環境維持隔離。實驗 IPC 目前仍需審視逾時、關閉／取消、worker 意外中斷及原始 OCR／圖片資料的記憶體生命週期。
5. **移除測試專用政策**：原 V6.6.7.6 子程序使用 `PII_MEDIA_FP_PROFILE=synthetic_face_test` 等固定合成測試設定。不能把這些旗標原封不動用於真實文件，否則有漏遮蔽風險。
6. 完成隔離整合後，重新執行合成 PDF 的 27/27 文字、17/17 已記錄人臉、PDF 安全結構和七頁 CPU/GPU 像素一致驗收，並測**真正** `ui.py` 中的單份 PDF 點擊→結果可見時間。
7. 在沒有完成真實文件風險測試、資料處理／還原流程驗收前，不將 GPU 預設開啟或部署到正式環境。

## 安全與隱私

唯讀分析四個 `.py` 檔，僅輸出一組固定名稱的檢查旗標與整數計數，不寫入原始碼、程式碼行、OCR 文字、文件內容、PDF、個資、路徑、模型、hash 或 private map；結果目錄必須在專案內且不能覆蓋 `.venv`、`.git`、`presidio`、相關安全工具目錄。若路徑經符號連結解析至外部，也會拒絕。

Windows 8.3 短路徑（例如 `C:\CONDA_~1`）比較使用 `.resolve()`，避免重演 V6.6.8.1 單元測試誤判。

## 後續需要回傳什麼？

請回傳 `v669_source_preflight.json`、`v669_source_preflight.md`，以及最新 `ui.py` 和 `core.py`。**請勿上傳包含真實個資的測試結果**。
