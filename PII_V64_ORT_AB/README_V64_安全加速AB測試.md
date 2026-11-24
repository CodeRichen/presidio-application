# V6.4：ONNX Runtime CPU 執行緒安全 A/B 測試

## 目標

在**不取消掃描 PDF 每頁 2×+3× OCR**、不修改 `handler_media.py`、`ui.py`、`ocr_backend.py` 或 PDF 安全輸出的前提下，測試 ONNX Runtime `intra_op_num_threads`（推論運算執行緒數）是否能改善完整文件處理速度。

ONNX Runtime 支援在建立 `InferenceSession` 前設定 `SessionOptions.intra_op_num_threads`。實際 RapidOCR 版號、模型數量及 CPU 核心數都會影響最佳設定；不能保證調低/調高必然加速。這份工具在**子程序中暫時攔截 Python ORT session 建立**，為候選組設定此值。若模型未經過此 Python API 建立 session，會回報 `TUNING_NOT_APPLIED`，而不是假裝成功。

參考：[ONNX Runtime Thread Management](https://onnxruntime.ai/docs/performance/tune-performance/threading.html)；[RapidOCR Engine Config](https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/parameters/)。

## 必備專案結構

解壓後，專案根目錄至少包含：

```text
presidio_app_allen/
  .venv/Scripts/python.exe
  handler_media.py
  pdf_secure_output.py            # 需已安裝 V5 patch
  pdf_secure_v5.py
  pdf_secure_v5_worker.py
  PII_Face_PDF_7page_Test/
    pii_face_7page_scan_source.pdf # 固定 SHA-256 的原始合成文件
  PII_V5_Security_Acceptance/
    v5_acceptance.py
    audit_7page_pdf.py
    run_7page_pdf_test.py
    expected_text_and_faces.json
  PII_V64_ORT_AB/
    v64_benchmark.py
    v64_child.py
    v64_runtime.py
    test_v64.py
```

**只處理原來那份七頁合成 PDF**。測試程式會在啟動前及子程序內核對 SHA-256，拒絕其他文件。原本的 `PII_MEDIA_FP_PROFILE=synthetic_face_test` 僅限這份合成測試，請勿用於真實文件。

## 指令（Windows PowerShell，專案根目錄）

先**關閉 GUI**，避免模型、CPU、RAM 同時使用造成測速失真。

### 1. 自我測試

```powershell
.\.venv\Scripts\python.exe .\PII_V64_ORT_AB\test_v64.py
```

### 2. 確認專案環境和合成 PDF 沒變

```powershell
.\.venv\Scripts\python.exe .\PII_V64_ORT_AB\v64_benchmark.py --check-only
```

### 3. 快速 A/B：基準設定 vs 4 個 ORT 執行緒

```powershell
.\.venv\Scripts\python.exe .\PII_V64_ORT_AB\v64_benchmark.py `
  --threads 4 --repeats 1 --results-dir .\v64_ort_results_quick
```

### 4. 若初測有差異，再做較可靠的兩組重複 A/B

```powershell
.\.venv\Scripts\python.exe .\PII_V64_ORT_AB\v64_benchmark.py `
  --threads 4 --repeats 2 --results-dir .\v64_ort_results
```

也可以將 `--threads 4` 分別改成 `2` 或 `8`，**每一個候選設定使用不同 `--results-dir`**，以免覆蓋報告。這些結果可能隨 Windows CPU 電源模式、其他程式負載、常駐模型而波動。正式採用前還要多次重測。

## 驗證原則

每次候選組都會使用與基準組**完全相同的** OCR、NER、FACE、PDF 輸出政策。流程：

1. 在獨立 Python 子程序內套用 ORT session 建立時的實驗執行緒數。
2. 執行原先 `PII_V5_Security_Acceptance/run_7page_pdf_test.py`，完整遮蔽合成七頁 PDF。
3. 使用原來的安全驗收：27/27 文字 PII 遮蔽覆蓋率 ≥98%、17/17 已記錄人臉框覆蓋率 ≥98%，檢查 PDF 附件、文字層、註解、連結、表單及頁面影像結構。
4. 將原版與候選版七頁 PDF 逐頁 150 DPI 渲染成 RGB，**像素必須完全一致**。
5. 確認 ORT `sessions_tuned > 0`，並比較完整流程耗時中位數，速度提升至少 10% 才標記 `PROMISING_AB_ONLY`。

這是**合成文件的功能/速度回歸**，不是正式文件全面安全證明；真實文件的人臉漏偵、PII 漏遮、誤遮蔽仍需另行驗證。V5 GUI 心跳延遲也不在這份工具的測量範圍。

## 可能的狀態

| 狀態 | 意義 |
|---|---|
| `PROMISING_AB_ONLY` | 已測到 ≥10% 加速，合成七頁完整回歸與像素一致；仍只准當候選 |
| `NO_CONFIRMED_GAIN` | 未達 10% 加速，或候選變慢 |
| `TUNING_NOT_APPLIED` | 未攔截到成功建立的 ORT session，不應宣稱此方案有作用 |
| `PIXEL_DIFFERENCE` | 候選版與原版顯示像素不同；**拒絕採用** |
| `SECURITY_GATE_FAILED` | 原版或候選版的遮蔽、PDF 結構或完整處理失敗 |
| `INCOMPLETE` | 尚未完成所有組別 |

任何執行失敗都不會改動正式程式；只有對固定合成文件的試驗環境變數被設置。

## 輸出資料與保護措施

```text
v64_ort_results/
  v64_ort_ab_summary.json
  v64_ort_ab_summary.md
```

模型可能將原始 OCR 結果印在標準輸出，因此**子程序標準輸出與錯誤輸出都丟棄**，永久報告只包含非敏感的數量、耗時、驗收摘要與 session 統計。含有未遮蔽裁圖的 `PRIVATE_restore_map_DO_NOT_SHARE.json` 及中間遮蔽 PDF 只放在 `%TEMP%\pii_v64_ab_*`，正常結束自動刪除。若程序被強制終止，請自行檢查 `%TEMP%` 並清理殘留。

完成後請只上傳 `v64_ort_ab_summary.json` 和 `.md`，**不要上傳私有還原 map**。若 `TUNING_NOT_APPLIED` 或 `NO_CONFIRMED_GAIN`，我們下一步會改測其他不降低 OCR 辨識次數的方法。
