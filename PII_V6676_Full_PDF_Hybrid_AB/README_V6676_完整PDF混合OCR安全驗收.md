# V6.6.7.6：混合 GPU OCR 完整七頁 PDF 安全 A/B（僅合成資料）

## 為何可以進入本輪

V6.6.7.5 的使用者實測：狀態 `ADAPTER_PREFLIGHT_OK`；第 1 頁 2×：CPU 與混合均 114 筆逐詞 OCR；3×：均 115 筆。文字、`left/top/width/height`、`line_id` 精確一致，`conf` 差異 ≤0.002。GPU/CPU 路由驗證：det CUDA 380 events、cls CPU 916 events、rec CPU 780 events，cls/rec CUDA 0。此結果**只涵蓋第一頁**，未完成全 PDF PII／人臉／PDF 結構與七頁像素驗收。

這一版保留 V6.6.7.5 **已驗證成功**的 GPU worker：呼叫 `RapidOCR(image, return_word_box=True)`，並使用現有專案 `ocr_backend.rapid_result_to_words()` 轉換；**不做自行拆字或平均分框**。完整 PDF A/B 也在執行前重新比對第一頁 2×/3×（Fail closed）。

## 工具與正式專案如何分工

| | 正式 CPU 組 | 混合組 |
|---|---|---|
| OCR det | 正式 `.venv` CPU | 獨立 `.venv_ocr_gpu` CUDA |
| OCR cls/rec | 正式 `.venv` CPU | 獨立 `.venv_ocr_gpu` CPU |
| 2×+3× OCR | 全部保留 | 全部保留 |
| PII、中文/英文模型、人臉與 V5 PDF 安全輸出 | 正式 `.venv` | 正式 `.venv` |
| `handler_media.py`、`ocr_backend.py`、正式 `.venv` | 不修改 | 不修改 |

這是**實驗階段的跨程序橋接**，不是官方 RapidOCR 設定，也不是正式程式的部署改版。

## 前置條件

1. 解壓本 ZIP，讓 `PII_V6676_Full_PDF_Hybrid_AB/` 位於 `presidio_app_allen` 專案根目錄；**不須保留舊版 V667 程式包**，本 ZIP 已包含所需的橋接及模型分工程式。
2. 正式 `.venv`、獨立 `.venv_ocr_gpu` 已建立。無須重新安裝 CUDA、ONNX Runtime、RapidOCR 或修改正式程式。
3. 現有 `ocr_backend.py` 具備 `rapid_result_to_words`（已由 V6.6.7.5 驗證）。
4. 原 V5 安全驗收工具 **完整** `PII_V5_Security_Acceptance/` 已放在專案根目錄（需 `v5_acceptance.py`、`run_7page_pdf_test.py`、`expected_text_and_faces.json` 等）。
5. 現有 `PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf` 必須是固定原版合成 PDF：SHA-256 `df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49`。**工具不允許使用真實文件或更換測試 PDF**。
6. 關閉正式 GUI，避免平行執行其他 PII/CUDA 重負載測試。

## Windows PowerShell 執行

請先在 `presidio_app_allen` 根目錄開啟 PowerShell：

### 1. 純工具單元測試（不跑 OCR 模型）

```powershell
.\.venv\Scripts\python.exe .\PII_V6676_Full_PDF_Hybrid_AB\test_v6676.py
```

應看到全部測試 `OK`。這是程式邏輯測試，不代表已通過整份 PDF 安全驗收。

### 2. 唯讀安裝預檢

```powershell
.\.venv\Scripts\python.exe .\PII_V6676_Full_PDF_Hybrid_AB\v6676_benchmark.py --check-only
```

如缺少 V5 gate、固定合成 PDF 或任一虛擬環境，先依預檢結果補齊原本檔案；**不要用空白檔替代，也不要修改 SHA-256**。

### 3. 先跑一組完整七頁 A/B

```powershell
.\.venv\Scripts\python.exe .\PII_V6676_Full_PDF_Hybrid_AB\v6676_benchmark.py `
  --repeats 1 `
  --results-dir .\v6676_full_pdf_smoke
```

這一版**先跑混合組**，如果 worker／Adapter／PDF 安全檢查失敗，就停止，不再浪費時間跑 CPU 對照組。混合組會先重新確認第一頁 2×、3× 逐詞 Adapter，然後執行完整七頁 PDF 的 PII、人臉、V5 安全輸出。成功後才跑 CPU 組，最後嚴格比對 7/7 輸出 PDF 的 150 dpi RGB 像素。

### 4. 一組安全通過之後才跑三組穩定性測試

```powershell
.\.venv\Scripts\python.exe .\PII_V6676_Full_PDF_Hybrid_AB\v6676_benchmark.py `
  --repeats 3 `
  --results-dir .\v6676_full_pdf_3pairs
```

三組會採用 **混合→CPU、CPU→混合、混合→CPU** 的交錯順序。任一組失敗即停止，不允許把部分測試當作成功。

## 安全 Gate 與結果判斷

- 固定合成文件：七頁、**2×+3×** 全部 OCR；不得省略高倍率 OCR。
- 混合測試前：第一頁 2×、3× 的逐詞 `text`、`left/top/width/height`、`line_id` 必須**精確一致**；`conf` 必須同時存在/不存在，數值差異 ≤0.002；否則停止。
- PDF 完整 OCR 預期呼叫：**14** 次（扣除預檢的兩次）。
- 模型路由：det CUDA Node > 0；cls/rec CPU Node > 0 且 CUDA Node = 0；det/cls/rec 各一個 Session，讀取三個 profiling 檔。
- CPU 和混合兩邊各自的 V5 驗收：固定 **27/27** 文字 PII GT、**17/17** 已記錄人臉框、PDF raster-only 安全結構通過。
- 完整輸出：CPU 與混合七頁 PDF 的 150 dpi RGB 像素要 **7/7 完全一致**，否則停止。
- 報告只包含時間、數量與固定狀態，不保存原始 PDF、OCR 原文/雜湊/框、私有還原 map 或 ONNX profile。中間資料在系統 `%TEMP%` 暫存區，正常執行結束會清理；**強制中止時請檢查** `%TEMP%` 的 `pii_v6676_*` 與 `pii_v6675_worker_*` 目錄。若等待中逾時，工具會嘗試結束子程序樹。

常見狀態：

| 狀態 | 意義與處理 |
|---|---|
| `INSUFFICIENT_REPEATS` | 一組完整安全驗收、像素相同；已有速度結果，但不能宣稱穩定加速 |
| `PROMISING_FULL_PDF_AB_ONLY` | 三組安全驗收全通過，且處理時間中位數改善 ≥10%，有至少兩組改善；**仍不可部署** |
| `NO_CONFIRMED_FULL_PDF_GAIN` | 完整安全驗收通過，但加速證據不足 |
| `OCR_ADAPTER_MISMATCH` | 預檢文字/框/行號/信心分數不符，停止遮蔽測試；看 `preflight_mismatch_kind` |
| `CHILD_EXCEPTION` | 看 `child_stage` / `error_type`，不需要上傳 private traceback |
| `HYBRID_PROVIDER_NOT_VERIFIED` | 實際 CUDA/CPU Node 分工未通過，停止 |
| `INCOMPLETE_DUAL_ZOOM_OCR` | 不是完整 14 次 2×+3× OCR，停止 |
| `SECURITY_GATE_FAILED` | 27/27、17/17、PDF 結構其中至少一項不通過 |
| `PDF_PIXEL_DIFFERENCE` | 即使已過其他 Gate，七頁像素仍與 CPU 不同，禁止採用 |
| `PDF_PIXEL_GATE_ERROR` | 像素比較未完成，禁止視為相同 |
| `TIMEOUT` | 子程序逾時，不能當作成功 |

## 怎麼讀計時

- `processing_s`：現有七頁 runner 的 `mask_pdf()` 處理計時，**不包含** GPU worker 建立、混合模式 OCR Adapter 預檢、子程序啟動、稽核。完整 PDF 處理期間的 GPU 溝通及影像編碼**包含在內**。
- `child_elapsed_s`：子程序啟動＋預檢＋PDF 遮蔽＋稽核的觀察時間，可評估實驗整體成本，**不能直接視為 GUI 的日常延遲**。混合組的嚴格預檢只在這個實驗中額外執行。
- V6.6.6 的 57.64% 是另外一輪的純 OCR 加速，**不是本次完整 PDF 改善幅度**。
- 即使時間低於 150 秒，也仍需通過全部 Gate 和至少三組配對；然後再規劃真實產品的部署方案與 GUI 回應性測試。

## 完成後只上傳兩份摘要

```text
v6676_full_pdf_smoke/
├── v6676_full_pdf_hybrid_summary.json
└── v6676_full_pdf_hybrid_summary.md
```

如果失敗，報告中會有 `reason`、`child_stage`、`error_type`、`preflight_mismatch_kind` 等**不含私有內容**的欄位。不要上傳任何 PDF、圖片、OCR 原文或私有 map。

**侷限**：27/27 是固定測試資料的已知 PII 區域；17/17 是已記錄人臉框，不等同於真實文件全面召回率。通過也只證明固定合成文件及目前環境的自動測試，仍需目視檢查與更多獨立測試。正式 `.venv` / `handler_media.py` / `ocr_backend.py` / GUI 都不修改。
