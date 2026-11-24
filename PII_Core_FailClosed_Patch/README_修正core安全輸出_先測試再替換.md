# core.py 失敗即停止修正（獨立測試副本）

## 改了什麼

1. **刪掉失敗時複製原稿的行為**：`_mask_media()` 只將 handler_media 產生的候選檔寫入私有暫存位置，零命中或異常不發布任何資料。
2. **批次不吞例外**：`dispatch_files()` 先處理完整批次，再逐一以 `os.link()` 原子無覆蓋發布；第二個發布失敗時撤銷本次已建立的檔案。*不刪除使用者預先存在的檔案*。
3. **資料夾不複製原稿樹**：`scan_folder()` 只在非公開的暫存樹下建立有遮蔽的結果，完成整批檢查後才以同檔案系統目錄 `rename` 發布。未知格式、未偵測出個資的檔案、略過資料夾，全部**不**出現在公開結果。若沒有命中，仍回傳一個不含原稿的空結果資料夾，以保持 GUI 介面格式。
4. **連還原對照表一起處理**：媒體 `map_file` 先寫私有檔案；出版成功後，再以原子寫入方式更新 `clipboard_map.txt` 和 `media_deid_map.json`；若寫入失敗就撤銷當次公開成果。原始裁切圖仍屬私密資料，不可分享或上傳。
5. **輸出 Gate**：拒絕空檔、原始檔位元組複製、非一般檔案、輸出已存在、已變更來源、已變更候選，以及未達黑色遮蔽的回報 bbox。PDF 另檢查頁數、文字層、附件、註解、連結、表單及每頁單影像結構。
6. **表格**：私有工作目錄中建立表格輸入副本供既有 handler_csv 讀取（工作完即刪除），無有效遮蔽回報則拒絕發布。
7. **日誌**：媒體與檔案遮蔽內部細節不再直接輸出原始 PII 字串到 UI 執行紀錄。

## 必須知道的限制

- **尚未在你的 Windows `.venv` 執行真正的 handler_media、RapidOCR、NER、YuNet 或 V5 七頁合成 PDF 完整驗收。** 這次通過的是對修改後 `core.py` 的**22 個合成／mock 故障與整合單元測試**，不能據此宣稱完整 PDF 安全、PII 零漏判或允許 GPU 正式上線。
- `os.link()` 需要輸出目錄所在的檔案系統支援 hard link（NTFS 通常支援）；否則**拒絕輸出**，不會自動降級成不安全的直接複製。
- 修改了資料夾掃描的語意：不再製作包含全部非 PII 檔案的完整資料夾複本，只包含**成功遮蔽的檔案**。GUI 原文字「複製整個資料夾」可能需要另改成「產生已遮蔽結果資料夾」。
- 此版仍沿用現有 NER、OCR、Regex、YuNet；本次只修檔案發布邏輯。**沒有**證明所有未被偵測到的個資都被安全遮蔽。
- 操作設計預設單一 GUI／工作程序，不包含多行程同時處理同一批檔案的併發保護、突然斷電後持久交易回復，也未處理惡意修改原始檔案的第三方程序。
- 私有暫存含媒體還原裁圖與 CSV 工作副本；測試結束會刪除。但如果 Python 被強制終止，請檢查專案資料夾中 `.pii_private_stage_...` 目錄，**勿將它們上傳或分享**。
- 既有 `handler_media.mask_pdf()` 的 OCR 漏判、PDF 中繼資料、其他安全檢查仍需你原本的 V5 完整實測與人工查核。結構 Gate 不是完整 PDF 安全稽核。

## 安裝方式：先測試，不要直接覆蓋正式程式

### 1. 將整個 `PII_Core_FailClosed_Patch` 資料夾放進 `presidio_app_allen` 專案根目錄

資料夾中的 `core.py` 是修正版，但**先不要複製到專案根目錄**。它並不在根目錄，所以原本 UI 仍會使用正式舊版。

### 2. 在 Windows PowerShell（專案根目錄）執行合成單元測試

```powershell
.\.venv\Scripts\python.exe .\PII_Core_FailClosed_Patch\test_core_failclosed.py
```

預期 `Ran 22 tests` 和 `OK`；測試會 mock 所有正式 app 模組，**不會真的載入你的 OCR/NER 模型，也不會改動正式對照表**。

### 3. 只檢查現有 core.py 是否能安全套用，不修改任何檔案

```powershell
.\.venv\Scripts\python.exe .\PII_Core_FailClosed_Patch\install_core_patch.py --check
```

預期 `SOURCE_MATCH: True`；若顯示來源版本不同，**不要強制覆蓋**，需重新比對差異。

### 4. 之後若要套用到正式程式（先關閉 GUI、備份專案）

```powershell
.\.venv\Scripts\python.exe .\PII_Core_FailClosed_Patch\install_core_patch.py --apply
```

工具會再度驗證來源 AST 與本次修正檔 SHA256；成功才在專案根目錄建立 `core.py.backup_YYYYMMDD_HHMMSS`，並原子替換 `core.py`。

**請勿因為 22 項合成測試通過就直接處理真實個資文件。** 套用後先對自己的假資料、單頁圖片及固定七頁合成 PDF 跑完整 GUI + PDF V5 驗證，確認功能沒壞且沒有額外漏判，再決定是否用於正式系統。

### 5. 回報結果

回傳 `python -m unittest` 的最末幾行、`install_core_patch.py --check` 的訊息，以及實際固定合成 PDF 的測試結果（不要上傳原稿／還原 map）。
