# V6.6.7.4：OCR Adapter 欄位結構診斷（修復 V6.6.7.3 的 KeyError）

## 先前報告的確定結論

- V6.6.7.2 確認 det 在 RTX 4060 的 CUDA Node 上執行，而且 RapidOCR 是**延遲建立**三個 ONNX Session。
- V6.6.7.3 狀態 `DIAGNOSTIC_FAILED`、階段 `COMPARE_2X`、例外 `KeyError`。
- V6.6.7.3 同時顯示 `cpu_schema_ok=false`、`hybrid_schema_ok=true`；這代表診斷器預設的 `{text,left,top,width,height,line_id}` 結構，至少有一項不符合**正式 CPU OCR 回傳**。
- V6.6.7.3 的 bug：`compare_without_text()` 在 schema 不合法時提前 return，沒有設定 `exact_adapter_equal`，但主迴圈繼續用 `info['exact_adapter_equal']`，因而 KeyError。
- **目前無法判斷 OCR 的原始文字內容是否不同，也無法宣稱完整 PDF 安全或效能已驗證。**

## 此工具修正什麼？

- 所有比對結果一定包含 `exact_adapter_equal`，若 schema 異常強制為 `false`，不會再發生前一版的 KeyError。
- 即使 2× 的正式回傳格式異常，仍繼續做第 1 頁 3× 的 OCR 比對。
- 各邊分開統計 `word_count`、`dict_rows`、`non_dict_rows`、六個固定欄位的缺失數量／型別異常數量、*額外欄位的筆數*（**不輸出額外欄位名稱**）。
- 僅當雙方 schema 都完整時，才在記憶體中進行嚴格文字／字框／`line_id` 比對與去空白文字串接比較；不放寬原本的嚴格相容門檻。
- 測試結束後核對 det CUDA、cls/rec CPU 的實際 Node 事件。
- **不會修改正式 `handler_media.py`、`ocr_backend.py`、模型、兩套虛擬環境或 V5 PDF 安全輸出**；不啟動完整七頁 PDF 遮蔽 A/B。

## 需要哪些既有檔案？

將整個 `PII_V6674_Adapter_Schema_Fix/` 資料夾解壓到 `presidio_app_allen` 專案根目錄，與 `handler_media.py` 同一層。

此工具需要：

- 專案原本的 `handler_media.py`、`ocr_backend.py` 和其依賴（依然使用正式 `.venv`）
- `.venv/Scripts/python.exe`、`.venv_ocr_gpu/Scripts/python.exe`
- `PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf`（SHA-256 固定為 `df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49`）
- Windows RTX 4060 的 CUDA Provider 可用，並保留現有 RapidOCR 3.10.0、ORT-GPU 1.31.1 的隔離環境

## Windows PowerShell 操作

請在 `presidio_app_allen` 的**專案根目錄**執行，關閉正式 GUI，勿同時使用真實文件：

**1. 離線單元測試（不載入實際 OCR 模型）：**

```powershell
.\.venv\Scripts\python.exe .\PII_V6674_Adapter_Schema_Fix\test_v6674.py
```

**2. 檢查路徑與固定合成資料：**

```powershell
.\.venv\Scripts\python.exe .\PII_V6674_Adapter_Schema_Fix\v6674_diagnose.py --check-only
```

**3. 執行第一頁 2×／3× 的欄位結構診斷：**

```powershell
.\.venv\Scripts\python.exe .\PII_V6674_Adapter_Schema_Fix\v6674_diagnose.py `
  --results-dir .\v6674_adapter_results
```

安全驗收不通過時，程序會**刻意回傳非零離開碼**；請看 JSON／MD 的 `status`、每個 zoom 的 `cpu` 與 `hybrid` 統計。不要因非零離開碼而關閉保護。

### 輸出檔案

```text
v6674_adapter_results/
├── v6674_adapter_diagnosis_summary.json
└── v6674_adapter_diagnosis_summary.md
```

## 診斷結果如何解讀？

- `OCR_ADAPTER_SCHEMA_MISMATCH`：六個固定欄位的存在性／型別或額外欄位與嚴格 Adapter 格式不同。首先檢查 `cpu.required_missing`、`cpu.required_wrong_type`、`cpu.unexpected_field_rows`、`cpu.non_dict_rows`。
- `OCR_ADAPTER_MISMATCH_CHARACTERIZED`：六個欄位都符合要求，但文字、次數、行號或字框仍有差異；不能正式部署。
- `PROVIDER_ROUTE_NOT_VERIFIED`：det CUDA、cls/rec CPU 的驗證證據不足；不能正式部署。
- `ADAPTER_PREFLIGHT_OK`：固定第一頁兩種 zoom 的回傳完全一致且 Provider 已驗證。**仍不代表 7 頁 PDF 安全驗收通過**。
- `DIAGNOSTIC_FAILED`：其他故障，使用 `probe.stage`、`probe.error_type` 確認類型。

注意：`cpu_schema_ok=false` 不等於正式 OCR 錯誤。這表示**實驗工具的 Adapter 假設與正式輸出不一致**。實驗 worker 是暫定轉換方式，在原始碼確認前不可據此修改正式版。

## 個資與實驗安全

- 僅允許固定 SHA-256 的合成七頁 PDF，且只處理第 1 頁 2× 和 3×。
- **不將 OCR 文字、原始圖片、完整／部分字框座標、文字雜湊、未知欄位名稱、私人還原 map 或 raw ONNX profile 寫入報告**。
- 所有影像與 OCR 原文只存於記憶體／匿名 pipe；子程序 stdout/stderr 丟棄。
- ONNX profiling 只在暫存目錄中分析，正常關閉時刪除。若強制結束，檢查 `%TEMP%` 內 `pii_v6674_` 和 `pii_v667_worker_` 前綴資料夾。
- 本工具不改動 2×+3× OCR、安全遮蔽政策、快取、正式 GUI 或 PDF 輸出程式。
- 任何無法嚴格一致的情況均不得視為安全。27/27 已標註文字 PII、17/17 已記錄人臉框與 PDF 結構／像素驗收**此版本沒有執行**。

## 已驗證與尚未驗證

- 33 項本機單元測試，包括重現缺少欄位造成的前版 KeyError，以及兩種 zoom 繼續比對的模擬整合案例。
- 模擬專案 `--check-only` 與 ZIP 檔案驗證（最後封裝時檢查）。
- **尚未在你的 Windows RTX 4060、真實 `ocr_backend.py` 和 RapidOCR 呼叫鏈上執行；實際差異需要你上傳本輪報告才能判定。**

## 回傳資料

完成後請上傳兩份 `v6674_adapter_diagnosis_summary` 報告。為了真的修正 Adapter，仍**需要現行** `ocr_backend.py` 和 `handler_media.py` 的原始碼（請先檢查程式內沒有寫死的密碼或憑證），否則只能定位、不能可靠修正正式輸出轉換。
