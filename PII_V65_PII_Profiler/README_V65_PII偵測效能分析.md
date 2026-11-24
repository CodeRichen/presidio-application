# V6.5：PII 偵測耗時分析與既有快取 A/B

## 為什麼先做這一步？

先前分析顯示七頁合成 PDF 處理約 200 秒，`pii.code_detect` 累計約 45 秒，值得量測是否存在可優化的重複工作。注意：目前 `handler_media.py` **已存在 `PII_MEDIA_CACHE` 頁面級快取**，所以不能再假設「新增加快取」會帶來改善。

本工具**不安裝任何新快取，也不修改正式系統**。只使用原本已驗證的七頁合成文件，保留 **OCR 2× + 3×**、原本的 PII／人臉規則、V5 PDF 安全輸出，量測：

- `handler_media.detect_matches`：包括既有快取命中／未命中在內的函式總耗時。
- `handler_code.detect`：實際進入原有 code/regex 敏感資訊偵測的次數與耗時。
- `handler_text.presidio_matches`：實際進入原有 Presidio/NER 程式的次數與耗時。
- 呼叫時期（2×／3× OCR）、文字長度區間、輸入重複比例、各函式時間 p95，以及**既有**頁面快取命中數。

**注意：各函式耗時是 inclusive time（含內層呼叫），不能直接相加；跨頁重複輸入比例不代表可跨文件快取。**

## 必備專案檔案

將此 ZIP 解壓到 `presidio_app_allen` 根目錄，保持下列路徑：

```text
presidio_app_allen/
  .venv/Scripts/python.exe
  handler_media.py
  pdf_secure_output.py         # 已套用 V5 patch
  pdf_secure_v5.py
  pdf_secure_v5_worker.py
  PII_Face_PDF_7page_Test/
    pii_face_7page_scan_source.pdf
  PII_V5_Security_Acceptance/
    v5_acceptance.py
    audit_7page_pdf.py
    run_7page_pdf_test.py
    expected_text_and_faces.json
  PII_V65_PII_Profiler/
    v65_profile_runtime.py
    v65_child.py
    v65_benchmark.py
    test_v65.py
```

如果你的 V5 測試工具只有 ZIP，請先解壓原本的 `PII_V5_安全回歸驗收工具包.zip`。程式會驗證輸入 PDF 的**固定 SHA-256**，以免錯誤地拿真實文件測試。測試只在子程序中啟用 `PII_MEDIA_FP_PROFILE=synthetic_face_test`，**此選項不可用於真實資料**。

## Windows PowerShell 執行指令

請先完全關閉目前 GUI，於專案根目錄執行：

### 1. 本機工具單元測試

```powershell
.\.venv\Scripts\python.exe .\PII_V65_PII_Profiler\test_v65.py
```

預期：`Ran 14 tests ... OK`。

### 2. 檢查 V5 工具與資料是否完整

```powershell
.\.venv\Scripts\python.exe .\PII_V65_PII_Profiler\v65_benchmark.py --check-only
```

預期：`CHECK_OK`。如果失敗，**不要覆蓋 `handler_media.py`**，請提供錯誤摘要。

### 3. 先跑一次正式設定的完整診斷（建議先做）

```powershell
.\.venv\Scripts\python.exe .\PII_V65_PII_Profiler\v65_benchmark.py `
  --results-dir .\v65_pii_results
```

本步只執行一次完整七頁測試，既有快取保持**開啟**，輸出耗時分析，不會宣稱新增加速效果。

### 4. 可選：A/B 評估「既有快取」是否真正有效

```powershell
.\.venv\Scripts\python.exe .\PII_V65_PII_Profiler\v65_benchmark.py `
  --compare-cache `
  --repeats 2 `
  --results-dir .\v65_pii_cache_ab_results
```

此指令在獨立 Python 子程序中分別測試現有 `PII_MEDIA_CACHE=1` 和暫時 `PII_MEDIA_CACHE=0`，各兩次，確認「現有快取」的貢獻。**即使快取開啟更快，也不是新優化成果，因為目前正式系統本來就有此功能。** A/B 會要求前後七頁渲染像素一致；不同即中止。

## 安全驗收與限制

每一次執行都必須通過原有安全驗收：已標註文字 PII **27/27**、已記錄 FACE-map 框 **17/17**、PDF 為無文字層的單影像頁面且無附件／註解／連結。這些驗收僅代表固定合成資料通過，不保證真實文件全面安全；人臉的 17 個框不是獨立的完整 face recall GT。

不允許以資料內容當成報告鍵值，也不保存字串雜湊。原始字串短暫存於子程序記憶體中供重複比例統計，程序結束即消失；原始 OCR、裁圖與還原 map 不會寫入最終報告。**子程序完整標準輸出與錯誤輸出都丟棄，避免既有程式的除錯輸出包含個資值。**

測試 PDF 輸出與 `PRIVATE_restore_map_DO_NOT_SHARE.json` 僅產生在 Windows 系統 `%TEMP%\pii_v65_profile_*`，正常離開會刪除。若你強制停止程式，可能殘留在暫存目錄中，應人工確認並刪除。不要上傳原始 map。

## 報告輸出

```text
v65_pii_results/
  v65_pii_profile_summary.json
  v65_pii_profile_summary.md
```

可能狀態：

| 狀態 | 解讀 |
|---|---|
| `PROFILE_ONLY` | 完成目前正式設定的 PII 效能診斷（無新加速部署） |
| `CACHE_AB_SYNTHETIC_ONLY` | 現有快取開／關 A/B 完成且輸出一致 |
| `SECURITY_GATE_FAILED` | 功能或安全驗收失敗，無法採用結果 |
| `PIXEL_DIFFERENCE` | A/B 輸出像素不同，拒絕採用候選 |
| `INCOMPLETE` | 執行未完成 |

上傳 `v65_pii_profile_summary.json` 和 `.md`，再依**實際**耗時決定下一步的程式修改：若 `code_detect` 最重，深入分析重複 regex／規則；若 `presidio_matches` 最重，分析 NER／Presidio；若已有快取命中不高且 PII 本身不是瓶頸，則停止在此花時間，轉向 OCR 效能優化。

## 與 V6.4 的關係

V6.4 的 ORT 4 執行緒測到約 4.28% 的中位數改善，但未達到 10% 門檻；**未部署**。這次是獨立診斷，不會自動套用 V6.4 設定、變更 OCR 次數、修改 GUI 或 PDF 匯出。
