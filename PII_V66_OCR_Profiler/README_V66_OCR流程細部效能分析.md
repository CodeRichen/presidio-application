# V6.6 — OCR 流程細部效能分析（安全影子診斷）

這個工具是 **診斷工具，不是加速補丁**。在你已通過 V5 驗收的 Windows `presidio_app_allen` 專案內，以固定 SHA-256 的七頁 **合成影像 PDF** 執行原本完整遮蔽流程，拆解 **2× / 3× OCR** 各階段耗時，找出下一個值得做 A/B 優化的環節。

## 與上一版的差別

- V6.5.3 顯示 PyTorch 14 → 4 執行緒在單組冷／暖測試中，純處理時間只改善約 0.99% / 1.17%。
- V6.6 不再嘗試調整模型執行緒，也不跳過 3× OCR。
- 目前 `handler_media.py` 的 `_collect_pdf_ocr_boxes` 可拆成 `Page.get_pixmap`、`Image.frombytes`、`preprocess_for_ocr`、`ocr_words`、`find_sensitive_boxes_in_words` 等階段。工具用**暫時性的函式包裝**計時，不修改源碼。
- `ocr_backend` 包含完整 OCR 呼叫／引擎轉換，**不能等同純神經網路推論時間**；`pii_boxes` 是辨識後的 PII 分析，裡面已包含 `code_detect`、`presidio_matches`，故不得把它們再加一次。
- 另外獨立計時 `_merge_pdf_multizoom_boxes` 及 `save_secure_raster_pdf`。`other_collect` 是函式中沒有被前五段涵蓋的剩餘時間（如 IP 補抓、座標轉換）；不保證它完全對應單一函式。

## 安裝

1. 先關閉正在執行的 GUI 與其他耗用 CPU 的工作。
2. 把 ZIP 解壓到 `presidio_app_allen` **根目錄**，資料夾名稱必須是 `PII_V66_OCR_Profiler`。
3. 維持目前已通過驗收的 `handler_media.py`、`pdf_secure_output.py`、`pdf_secure_v5.py`、`pdf_secure_v5_worker.py`、模型與 V5 安全回歸工具包，不要覆蓋。
4. 確認固定七頁合成 PDF 位於：`PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf`。

```text
presidio_app_allen/
├─ .venv/Scripts/python.exe
├─ handler_media.py                      # 既有，完全不修改
├─ pdf_secure_output.py                  # 既有 V5
├─ pdf_secure_v5.py                       # 既有 V5
├─ pdf_secure_v5_worker.py                # 既有 V5
├─ PII_Face_PDF_7page_Test/
│  └─ pii_face_7page_scan_source.pdf
├─ PII_V5_Security_Acceptance/
│  ├─ v5_acceptance.py
│  ├─ run_7page_pdf_test.py
│  ├─ audit_7page_pdf.py
│  └─ expected_text_and_faces.json
└─ PII_V66_OCR_Profiler/
   ├─ v66_runtime.py
   ├─ v66_child.py
   ├─ v66_benchmark.py
   ├─ test_v66.py
   └─ README_V66_OCR流程細部效能分析.md
```

如果 `PII_V5_Security_Acceptance` 目前只有 ZIP，先把過去的 `PII_V5_安全回歸驗收工具包.zip` 解壓到根目錄。**不要重新下載或更換測試 PDF**；此工具會拒絕 SHA-256 不吻合的文件，以防意外處理真實個資。

## 執行步驟（Windows PowerShell）

請先 `cd` 到 `presidio_app_allen` 專案根目錄。

**1. 單元測試（不載入模型）**

```powershell
.\.venv\Scripts\python.exe .\PII_V66_OCR_Profiler\test_v66.py
```

預期：`Ran 16 tests ... OK`（也可能因未來新增單元測試而改變數量）。

**2. 檢查固定合成測試資料與 V5 工具**

```powershell
.\.venv\Scripts\python.exe .\PII_V66_OCR_Profiler\v66_benchmark.py --check-only
```

預期：`CHECK_OK`。此步不執行 OCR。

**3. 執行一次完整 OCR 流程分析（建議先跑）**

```powershell
.\.venv\Scripts\python.exe .\PII_V66_OCR_Profiler\v66_benchmark.py `
  --repeats 1 `
  --results-dir .\v66_ocr_results
```

**4. 可選：再測兩次看耗時變化**

```powershell
.\.venv\Scripts\python.exe .\PII_V66_OCR_Profiler\v66_benchmark.py `
  --repeats 2 `
  --results-dir .\v66_ocr_results_repeat2
```

每次約為一次完整七頁 PDF 的工作量，**尚不能提供實際耗時預估或保證任何加速**。如單一子程序因環境問題執行超時，可加入 `--timeout 1200`；單位為秒。

## 驗收機制

每一輪都必須：

- 完整執行 **7 次 2× 與 7 次 3×** OCR 收集、渲染、影像轉換、前處理、辨識與 PII 框判斷；少一個階段即標示 `INSTRUMENTATION_FAILED`。
- 確認固定合成測試的 **27/27** 已標註文字 PII、**17/17** 已記錄人臉框覆蓋，以及 V5 的安全重建 PDF 結構 `PASS`。
- 若 `--repeats 2` 或 `3`，不同輪次輸出 PDF 在 150 DPI 逐頁渲染時必須 **7/7 像素一致**；不一致標示 `PIXEL_DIFFERENCE`。
- 保留雙解析度、原本的 PII 偵測規則、人臉設定、頁面快取與安全 PDF 輸出。

**注意**：27/27、17/17 是固定合成 GT 的自動驗收，不能證明真實文件沒有漏判，亦不代表獨立的人臉偵測召回率。

## 結果檔案

```text
v66_ocr_results/
├─ v66_ocr_profile_summary.json
└─ v66_ocr_profile_summary.md
```

報告列出：每個階段的 **2×、3× 合計時間**、每頁匿名數字計時、完整處理時間、PDF 安全輸出時間、其他時間與占比。**不保存** OCR 原文、影像裁切、PII 值、原始人臉圖片、函式引數、私人對照表或文字雜湊。

輸出狀態：

| 狀態 | 意義 |
|---|---|
| `PROFILE_OK_SINGLE` | 一輪分析完成、已通過固定合成安全驗收，尚無重複性證據 |
| `PROFILE_OK_REPEATED` | 兩輪或三輪分析完成且像素一致；仍不是加速 A/B |
| `CHILD_RUN_FAILED` / `TIMEOUT` | 子程序失敗／超時；診斷不完整 |
| `INSTRUMENTATION_FAILED` | 應有的 2×、3× 計時階段缺失；拒絕判斷瓶頸 |
| `SECURITY_GATE_FAILED` | GT 或安全 PDF 結構不通過；拒絕採用 |
| `PIXEL_DIFFERENCE` | 多次輸出的遮蔽像素不同；先調查原因 |

### 資安注意事項

- 報告檔案可傳回分析，但**不要傳**私有 map 或未遮蔽文件。
- 真正的合成測試 PDF、私人 map、遮蔽輸出 PDF 只產生在系統暫存目錄 `%TEMP%\pii_v66_*`；正常退出即自動刪除。
- 子程序的 stdout/stderr 不會寫入檔案，避免現有模組可能輸出的敏感字串被保存。
- **如果強制中止程式**，Python 可能無法清除暫存檔，請檢查 `%TEMP%` 中 `pii_v66_` 開頭的目錄並自行刪除。
- 若測試失敗請只提供狀態碼或去識別化後的錯誤訊息，不要貼含 PII 的完整運行日誌。

## 看完報告的下一步

- `ocr_backend` 最高：檢查 RapidOCR 引擎內部模型初始化、文字偵測／辨識、推論資源或影像切分；另建 A/B。
- `preprocess` 最高：研究影像格式轉換／濾鏡流程，但必須驗證像素等價和 PII 安全性。
- `pdf_render`、`pixmap_to_pil` 最高：研究渲染與複製成本，仍保留原本 2×+3× 的辨識品質。
- `pii_boxes` 最高：針對去識別化運算做進一步微型診斷，不能直接跳過 NER。
- `secure_export_wall_s` 偏高：回頭檢查 V5 安全輸出，但不能改回含原始物件的 PDF 儲存方式。

**本工具只用於找出真正瓶頸，沒有安裝任何正式加速方法。**
