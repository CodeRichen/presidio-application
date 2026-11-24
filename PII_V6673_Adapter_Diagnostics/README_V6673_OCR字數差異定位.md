# V6.6.7.3：正式 OCR 與混合 GPU Worker 的字數差異定位

## 目前證據

- V6.6.6：固定七頁合成 PDF，14/14 組 **RapidOCR 原始 OCR 輸出**在全 CPU／混合 GPU 模式下相容；混合模式純 OCR 約快 57.64%（單輪，非完整 PDF）。
- V6.6.7：完整 PDF 混合子程序 `CHILD_FAILED`，未產生有效安全 A/B 結果。
- V6.6.7.2：已確認 det 真正執行 CUDA，合成 OCR 後 det／cls／rec Session 均建立；但**原本 `handler_media.ocr_words()` 與實驗 worker 的字典筆數不同**，停在第 1 頁 2× `WORD_COUNT_DIFFERENCE`。
- **目前仍未取得最新版 `ocr_backend.py` 的原始程式內容，因此不能安全假定兩套 OCR 的分詞規則一樣。**

這套工具**不是修復補丁**。它只回答：正式 CPU OCR 與混合 worker 在同一份影像上的差異，主要是分詞／分行，還是文字內容已經不同。

## 會執行哪些操作？

1. 驗證 `PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf` 的固定 SHA-256，不符即拒絕。
2. 使用原本 `.venv` 中的 `handler_media.preprocess_for_ocr` 建立第 1 頁 **2× 和 3×** 影像。
3. 分別送入正式 `handler_media.ocr_words()`（固定 `PII_OCR_BACKEND=rapid`）與獨立 `.venv_ocr_gpu` 的混合 worker。
4. **只在記憶體中**逐筆比對原本的 `text`、`left`、`top`、`width`、`height`、`line_id`；報告不儲存任何原文、雜湊或座標。
5. 即使發現第一組不相等，仍嘗試第二組（3×），以取得完整的對照筆數。
6. 完成後驗證 det／cls／rec 的 CUDA／CPU Provider Node events。正式程式與所有環境都保持不變。

## 報告內容

對每個解析度記錄：

- 正式版與混合版的 OCR 項目數（`word_count`）
- 分組行數（`line_id_groups`）
- 總字元數、不含空白字元數及含內部空白的項目數（只有**總計**）
- **`ordered_text_equal_after_whitespace_removal`**：把所有 OCR 項目依原順序串接、移除空白後，兩邊是否一樣。只留下 `True`／`False`；計算過程不留 OCR 文字或其雜湊。
- 最早出現的差異型別與模型執行路由驗證結果。

解讀：

| 結果 | 可能原因／處理方向 |
|---|---|
| `word_count` 不同，但去空白後串接文字相同 | 可能為拆詞、分行、空白過濾方式不同；**仍不代表座標安全等價** |
| `word_count` 不同，去空白後文字也不同 | 可能是模型參數、過濾門檻、排列順序或 OCR 內容不同，需查看 `ocr_backend.py` |
| 項目數相同但 `line_id` 不同 | 分行策略不同，可能影響下游 PII 偵測 |
| 文字相同但字框不同 | 可能為座標型別、取整或框轉換不同；不可直接部署 |
| `ADAPTER_PREFLIGHT_OK` | 第 1 頁兩種解析度逐字典精確相同、模型路由通過；仍**不等於完整 PDF 驗收通過** |

**安全門檻不放寬：**移除空白後文字相同只是診斷資訊，不會被當作精確相容。未通過 `ADAPTER_PREFLIGHT_OK` 前，不得將混合 OCR 接入正式 PDF 遮蔽。

## 安裝與執行（Windows PowerShell）

把 ZIP 解壓到 `presidio_app_allen` **專案根目錄**。需要：

- 專案原本的 `handler_media.py`、`ocr_backend.py`。
- 兩個既有虛擬環境：`.venv` 和 `.venv_ocr_gpu`。
- 前一階段已驗證的固定合成 PDF，路徑必須完全一致。
- 不必修改 V6.6.7、V6.6.7.1 或 V6.6.7.2；也不用重新安裝 CUDA 套件。

### 1. 本機單元測試（不啟動 GPU 模型）

```powershell
.\.venv\Scripts\python.exe .\PII_V6673_Adapter_Diagnostics\test_v6673.py
```

### 2. 僅檢查檔案與 SHA-256

```powershell
.\.venv\Scripts\python.exe .\PII_V6673_Adapter_Diagnostics\v6673_diagnose.py --check-only
```

### 3. 執行字數差異定位

```powershell
.\.venv\Scripts\python.exe .\PII_V6673_Adapter_Diagnostics\v6673_diagnose.py `
  --results-dir .\v6673_adapter_results
```

如果因為驗收未通過而回傳 PowerShell 非零離開碼，這是預期的 fail-closed 行為，請查看摘要中的狀態。

## 請上傳給 ChatGPT

```text
v6673_adapter_results/
├── v6673_adapter_diagnosis_summary.json
└── v6673_adapter_diagnosis_summary.md
```

若診斷指出分詞／分行差異或文字流不同，請**一併上傳現行專題**的 `ocr_backend.py` 與 `handler_media.py`。這兩個 Python 原始碼通常不包含實際文件內容，但上傳前仍可自行檢查是否有硬編碼的密碼或憑證。取得原始碼才能實作**有依據的實驗 adapter 修正**；不能猜測或強迫 CPU/GPU OCR 結果相等。

## 隱私與安全注意事項

- **只用固定合成 PDF**。不得用於真實文件，也不要在正式 GUI 設定 `PII_MEDIA_FP_PROFILE=synthetic_face_test`。
- 影像與 OCR 字串只存在子程序／worker 的記憶體管道；不寫入 JSON／MD、PDF、文字雜湊、座標、圖片、私人 map 或原始 ONNX profile。
- 子程序的 stdout／stderr 被抑制，報告只保留安全欄位和例外**型別**，不保存可能含個資的錯誤訊息或 traceback。
- ONNX Runtime 原生 profile 只存在暫存資料夾，正常結束時清理。強制中止時，檢查 `%TEMP%` 下 `pii_v6673_` 和 `pii_v667_worker_` 前綴資料夾並清除。
- 沒有執行 27/27 文字 PII、17/17 已記錄人臉框、V5 PDF 結構與逐頁像素比對；**不能把本次測試當成完成加速或已安全部署的證據**。

## 工具驗證範圍

- 28/28 本機 Python 單元測試通過：字數／文字流／欄位差異、模型路由、報告白名單、匿名二進位管道和原本安全等價門檻。
- 模擬專案 `--check-only` 通過：固定 PDF SHA-256 與必備檔案結構檢查。
- **未**在使用者的 Windows RTX 4060、最新版 `ocr_backend.py` 上執行 OCR；實際差異待使用者測試報告。
