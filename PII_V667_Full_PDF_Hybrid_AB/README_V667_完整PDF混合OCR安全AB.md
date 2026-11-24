# V6.6.7：混合 CPU/GPU OCR 完整 PDF 安全 A/B（僅合成測試）

## 這次測什麼？

- CPU 模式：在原本 `.venv` 中使用既有 `ocr_backend.py`，完整跑 PII、臉部遮蔽及 V5 PDF 安全輸出。
- Hybrid 模式：原本 `.venv` 仍負責 PII、臉部、V5 安全輸出；RapidOCR 另外在 `.venv_ocr_gpu` 子程序運作，**det 走 CUDA、cls/rec 走 CPU**。
- CPU/GPU 交換 OCR 影像與文字只走**匿名記憶體管道**。不寫入中間 OCR 文字、文字 hash、私有 map、截圖或 ONNX profile 到報告。
- 模型和輸出策略不變：完整 2×+3× OCR、原本 PII/人臉及 V5 安全重建。
- **僅允許** SHA-256 `df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49` 的 `PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf`。禁止拿真實文件測試。

## 使用前條件

1. 已完成 V6.6.6：獨立 `.venv_ocr_gpu` 的 RapidOCR 3.10.0、ONNX Runtime GPU 1.31.1 可執行 CUDA。
2. 已保留原本正式 `.venv`，以及專案根目錄的 `handler_media.py`、**`ocr_backend.py`**、`handler_code.py` 等現有檔案。
3. 已安裝完整 `PII_V5_Security_Acceptance/` 與 V5 子程序 PDF 安全輸出檔案。
4. 將本 ZIP 的 `PII_V667_Full_PDF_Hybrid_AB/` 放在 `presidio_app_allen` 根目錄；關閉 GUI。
5. **不需要也不應修改正式 `.venv` 或安裝新套件。** 不要套用任何測試中的 synthetic_face_test 設定到正式執行環境。

## 指令（Windows PowerShell）

```powershell
# 1. 不啟動模型，先測工具
.\.venv\Scripts\python.exe .\PII_V667_Full_PDF_Hybrid_AB\test_v667.py

# 2. 唯讀環境檢查，查找固定合成 PDF 和兩套 Python
.\.venv\Scripts\python.exe .\PII_V667_Full_PDF_Hybrid_AB\v667_benchmark.py --check-only

# 3. 一組 smoke A/B，完成兩次七頁完整 PDF 流程
.\.venv\Scripts\python.exe .\PII_V667_Full_PDF_Hybrid_AB\v667_benchmark.py `
  --repeats 1 `
  --results-dir .\v667_full_pdf_smoke

# 4. smoke 若成功，才執行三組交錯 A/B，確認穩定性
.\.venv\Scripts\python.exe .\PII_V667_Full_PDF_Hybrid_AB\v667_benchmark.py `
  --repeats 3 `
  --results-dir .\v667_full_pdf_3pairs
```

完整測試需載入 PII 模型與人臉模型，而且要執行 2×+3× OCR；不必同時做其他 CPU/GPU 高負載工作。

## fail-closed 設計

混合模式啟動時，**先在第 1 頁的 2× 與 3× 影像上執行 CPU/hybrid OCR**，逐筆驗證既有 `ocr_backend.py` 的 `ocr_words()` 輸出與 GPU 橋接轉換的 `text/left/top/width/height/line_id` 是否**完全一致**。這是保守、嚴格的接口檢查。

**由於目前尚未取得你最新版 `ocr_backend.py` 的原始內容**，橋接器採用 RapidOCR `txts`/`boxes` 的標準邊界框轉換。如果原專案做了不同的文字切分、四捨五入或行編號，可能出現：

- `OCR_ADAPTER_MISMATCH`：停止，不執行混合 PDF；**請上傳 `ocr_backend.py` 與 `handler_media.py`** 以依實際接口修正橋接器。這個失敗不代表 GPU 辨識不準確，也不代表遮蔽不安全。
- `HYBRID_PROVIDER_NOT_VERIFIED`：即使 Session 有 CUDA，也需在 ORT profile 的 det Node 看到 CUDA，cls/rec Node 只能是 CPU；否則拒絕結果。
- `SECURITY_GATE_FAILED`：27/27 文字 GT、17/17 *已記錄 map 人臉框* 與 PDF 安全結構若有任一失敗，停止。
- `PDF_PIXEL_DIFFERENCE`：CPU 和 Hybrid 的七頁安全 PDF 以 150 dpi 逐像素比較，不完全一致就停止。
- `INSUFFICIENT_REPEATS`：一至兩個安全配對通過，但不適合宣稱穩定加速。
- `PROMISING_FULL_PDF_AB_ONLY`：三個安全配對均通過，純處理時間中位數改善至少 10% 且有 ≥2 組配對改善；**不代表已可部署**。
- `NO_CONFIRMED_FULL_PDF_GAIN`：通過安全驗收，但加速證據不足。

## 這次計時的限制

`processing_s` 是既有七頁 runner 中的 `mask_pdf()` 函式計時，不包含 GPU worker 啟動、模型載入、CPU/Hybrid OCR adapter 預檢，也不包含稽核。候選模式 PDF 流程中的圖片編碼／管道傳送時間**有包含**。`child_elapsed_s` 包含這些準備成本與稽核，適合觀察完整子程序測試開銷，但包含嚴格預檢，**不是使用者日常 GUI 的實際等待時間**。兩個模式都在獨立的原始 `.venv` 主程序測試；GPU 環境只是 OCR worker。

V6.6.6 的純 OCR 57.64% 不是 V6.6.7 的完整流程加速百分比。所有節省都必須實測，不能推算當作達成 150 秒。

## 安全與隱私

- 中間測試 PDF、具有私人內容的還原 map、完整 OCR 資料與原始 ONNX profile，**只存在暫存目錄或程序記憶體**；正常退出時清除。
- 正式報告 `v667_full_pdf_hybrid_summary.json` / `.md` 只含數字和固定狀態碼，可以上傳。**不要上傳** PDF 輸出、私有 map、暫存 GPU profiles 或任何 OCR 原文。
- 過程遭強制終止（含逾時）時，檢查 `%TEMP%` 下的 `pii_v667_` / `pii_v667_worker_` 暫存資料夾；也要確認沒有殘留的 worker Python 程序。
- 這些驗收只涵蓋固定合成案例：27/27 已標註文字 GT、17/17 已記錄人臉 map 框；**不等於真實文件上的零漏判或完整人臉召回率**。
- 沒有修改正式推論程式、正式 Python 環境或正式 GUI。仍需另外驗證 GUI 回應性與多種合成樣本。

## 上傳檔案

```text
v667_full_pdf_smoke/
├── v667_full_pdf_hybrid_summary.json
└── v667_full_pdf_hybrid_summary.md
```

**先跑一組 smoke，結果若是 adapter 不相容，請先上傳 `ocr_backend.py`、`handler_media.py`，不要反覆跑三組。**
