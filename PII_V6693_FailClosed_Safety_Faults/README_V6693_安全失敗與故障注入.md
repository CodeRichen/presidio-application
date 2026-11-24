# V6.6.9.3｜安全失敗（Fail-Closed）與故障注入：**隔離測試階段**

本工具針對你目前的 `ui.py`、`core.py` 做**唯讀 AST 原始碼審查**，以及在標準函式庫的暫存資料夾內進行**全合成的失敗注入**。**不會 import 你的專案、不啟動 GUI、不載入 OCR／GPU／NER／YuNet、不開啟 PDF、不改動虛擬環境、不修改 `ui.py` 或 `core.py`。**

> ⚠️ **這不是正式 GUI 或核心程式的安全修補，不可用於真實文件，也不能視為真實 PDF 去識別化的安全驗收。** 之前 V6.6.9.2 的安全 A/B 是固定七頁合成 PDF；本階段故障注入使用的是**假的 PDF 位元組與注入式假處理器／假稽核**。

## 安裝

將整包解壓在 `presidio_app_allen` 專案根目錄，保留以下路徑：

```text
presidio_app_allen/
├─ ui.py
├─ core.py
├─ .venv/Scripts/python.exe
└─ PII_V6693_FailClosed_Safety_Faults/
   ├─ v6693_failclosed.py
   ├─ test_v6693.py
   └─ README_V6693_安全失敗與故障注入.md
```

## 步驟一：離線單元測試

在 PowerShell（目前位於專案根目錄）執行：

```powershell
.\.venv\Scripts\python.exe .\PII_V6693_FailClosed_Safety_Faults\test_v6693.py
```

本機測試為 **24/24 OK**；Windows 仍以你執行結果為準。

## 步驟二：唯讀檢查正式程式中的風險位置

```powershell
.\.venv\Scripts\python.exe .\PII_V6693_FailClosed_Safety_Faults\v6693_failclosed.py --check-only
```

在你先前上傳的版本中，預期三個風險為：

```text
V6693_STATUS: STATIC_RISK_REVIEW_READY
CONFIRMED_CORE_EXCEPTION_CONTINUES: True
CONFIRMED_CORE_EXCEPTION_COPIES_SOURCE: True
CONFIRMED_SCAN_COPIES_SOURCE_TREE: True
NO_GUI_NO_OCR_NO_PDF_PROCESSED
```

注意：風險欄位的 **True 代表原始程式中存在需要修正的風險**，不是安全檢查 PASS。如果本機版本與你上傳的版本有差異，以本機實際結果為準。

## 步驟三：15 項全合成故障情境

```powershell
.\.venv\Scripts\python.exe .\PII_V6693_FailClosed_Safety_Faults\v6693_failclosed.py `
  --results-dir .\v6693_fault_results
```

預期輸出：

```text
V6693_STATUS: FAULT_MOCK_TESTS_PASS_SOURCE_REVIEW_REQUIRED
MOCK_FAULT_PASS: 15/15
PRODUCTION_GPU_ALLOWED: False
```

工具只測試：成功模擬案例、GPU worker 崩潰、OCR 逾時、PDF 輸出例外、部分輸出後失敗、沒有產出、零長度產出、稽核拒絕與稽核丟例外、來源白名單、來源雜湊失配、第二項失敗回滾、錯誤的稽核回傳型別、符號連結候選輸出，以及來源資料未被修改。

隔離防護原型採用**先驗證所有來源、只在暫存目錄處理、只接受獨立的 `audit(...) is True`、整批過程中不對外發布任何檔案**。所有候選輸出在暫存目錄離開時刪除。這是示範「失敗時不會發布結果」的最小契約，不能取代正式 PDF 像素與隱藏資料檢驗。

## 產出報告

將以下兩個檔案上傳：

```text
v6693_fault_results/
├─ v6693_failclosed_summary.json
└─ v6693_failclosed_summary.md
```

報告**只存固定代號、布林值和成功數量**，不保存 OCR 文字、真實路徑、PII、對照表、原始 PDF、影像或例外細節。為避免覆寫，若曾執行過，請另設 `--results-dir .\v6693_fault_results_02`。

## 正式 GUI 的已知待修正項目（此次並未修改）

1. `core._mask_media`：指定 `out` 的例外分支包含 `shutil.copy2(path, out)`；如果 `out` 被誤當安全輸出，會洩漏未遮蔽內容。**不得只刪除 `copy2` 就認為問題修完**，因為底層遮蔽可能已經寫下部分輸出。需使用暫存私有路徑與通過完整安全 Gate 後才發布。
2. `core.dispatch_files`：圖片／PDF 失敗時 `except ... continue`；整批作業不能在部分失敗時顯示整體成功。需有逐檔 `FAILED` 狀態，並區分完整成功、部分成功和失敗；實驗 GPU 模式建議整批 fail-closed。
3. `core.scan_folder`：`shutil.copytree(folder, dest)` 先複製整棵原始資料夾，包含未遮蔽內容。資料夾副本**不能**當作去識別化安全成果；實驗 GPU 模式應先停用這個入口，將來改成私有 staging → 全部稽核通過 → 原子發布安全成果。
4. 正式輸出還牽涉到 map、敏感清單、已存在檔案及剪貼簿；正式整合時需另外確認這些副作用在失敗時如何處理，不能只以 PDF 存在判斷安全。

### 下一關（V6.6.9.4）

在確認上述故障注入報告後，製作**依你目前的原始碼定點修改的測試副本**，先實作分檔錯誤狀態、檔案輸出交易、資料夾模式停用及真實固定合成 PDF 的故障注入，再重跑 V6.6.9.2 的安全 A/B。**正式來源檔和 `.venv` 維持不變，混合 GPU 不處理真實文件。**
