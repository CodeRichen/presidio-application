# V6.6.9.4：隔離 Core 安全輸出交易與批次回滾測試

## 本階段定位

V6.6.9.3 的報告確認：15/15 合成故障注入通過，**但正式 `core.py` 仍可能：**

1. `dispatch_files()` 於媒體處理例外後 `continue`，整批結果可能看似正常結束。
2. `_mask_media(..., out=...)` 遇到例外時 `shutil.copy2(path, out)`，可能將原始未遮蔽內容放到指定輸出。
3. `scan_folder()` 先複製原始目錄；若 UI 讓使用者誤認複本已安全，可能造成暴露。

**本 ZIP 不會覆蓋或 patch 正式的 `core.py`、`ui.py`、`handler_media.py`，也不會執行 OCR、CUDA、PDF 安全稽核或正式 GUI。** 這是 `core` 層安全交易的*可執行隔離設計副本*，不是可直接換上的 `core.py`。

## 交易安全契約

`v6694_core_preview.py` 會：

- 只接受*專為本測試建立*、含有 `V6694_SYNTHETIC_FIXTURE` 標記、路徑及 SHA-256 皆已列入允許清單的**假文件**；真實 `%PDF` 檔會直接被拒絕。
- 讓處理器**只能**向 private staging 路徑產生候選輸出（本階段處理器為 mock）。
- 所有候選檔先通過**獨立注入的 mock auditor**，並要求 `audit(...) is True`（不接受 `"PASS"` 或 `1` 代替）。
- 所有候選稽核通過、來源雜湊不變，才進行批次 commit。
- Commit 使用 `os.link()` 建立不可覆蓋的輸出連結；如果第二項 commit 失敗，回滾本批次已建立的連結。若原有檔案存在，拒絕覆蓋、亦不刪除它。
- 在 `TemporaryDirectory` 中建立所有 staging 與 published 檔案；**測試結束一律刪除**，不保存任何可復原檔案或影像。
- 報告只保留情境、通過與否、固定的 AST 風險布林值、數量，不記錄合成輸入內容、OCR 文本、實體座標、private map、PDF 或資料路徑。

**侷限：**此 mock auditor *不是* V5 PDF 稽核；尚未驗證模型、實際 OCR 或 PDF，且對 OS 強制殺死程序、突然斷電、惡意修改來源的程序、跨程序競爭及 crash-recovery 尚無正式安全保證。真實應用整合前須另外解決這些議題與還原 map / 敏感清單的交易一致性。

## 解壓與操作（Windows PowerShell）

將 `PII_V6694_Synthetic_Transactional_Core` 解壓到 `presidio_app_allen` 根目錄，與 `core.py`、`ui.py` 同層。**不需要開啟正式 GUI**。

### 1. 單元測試

```powershell
.\.venv\Scripts\python.exe .\PII_V6694_Synthetic_Transactional_Core\test_v6694.py
```

預期：`Ran 15 tests`、`OK`。

### 2. 唯讀原始碼檢查

```powershell
.\.venv\Scripts\python.exe .\PII_V6694_Synthetic_Transactional_Core\v6694_fault_runner.py --check-only
```

檢查原始碼的 AST，不 import 正式 `ui.py` 或 `core.py`；輸出是否讀到所需掛接點，但**不是 GPU 可上線的許可**。

### 3. 執行 23 項合成交易／故障注入

```powershell
.\.venv\Scripts\python.exe .\PII_V6694_Synthetic_Transactional_Core\v6694_fault_runner.py `
  --results-dir .\v6694_fault_results
```

預期：

```text
V6694_STATUS: SYNTHETIC_TRANSACTION_MOCK_PASS_SOURCE_REVIEW_REQUIRED
MOCK_SCENARIOS: 23/23
PRODUCTION_GPU_ALLOWED: False
```

如果結果目錄裡已有同名報告，本程式會拒絕覆寫。請指定新的 `--results-dir`。

完成後上傳：

```text
v6694_fault_results/
  v6694_safe_transaction_summary.json
  v6694_safe_transaction_summary.md
```

## 下一階段 V6.6.9.5（本 ZIP 沒有執行）

以**獨立程式副本**將安全交易機制接入已通過 V6.6.9.2 的固定七頁合成 PDF 流程，使用真正的 V5 PDF 獨立稽核及 GPU worker，不再使用 mock；並測試崩潰/超時、PDF 輸出、PDF 逐像素一致及 GUI 結束狀態。應先重新審查 `core.py` 的輸出副作用與還原 map。完成安全驗收後才考慮正式版的 feature flag / CPU fallback。

**禁止把 `v6694_core_preview.py` 重新命名成 `core.py`。**
