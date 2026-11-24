# UI 端到端測速與 PDF 安全回歸測試（Windows / 離線）

本工具包不會修改 `ui.py`、`core.py`、`handler_media.py` 或 OCR/NER 模型。
它包含真正的 UI 操作計時器、掃描 PDF 的內容／物件檢查、合成測試資料，以及會被安全檢查抓到的**故意不安全範例**。

## 0. 解壓縮與環境

將 `PII_UI_PDF_Security_Kit` 整個放到 `presidio_app_allen` 專案根目錄：

```
presidio_app_allen/
  ui.py
  core.py
  handler_media.py
  .venv/
  PII_UI_PDF_Security_Kit/
    launch_ui_timed.py
    summarize_timing.py
    audit_pdf_security.py
    run_pdf_regression.py
    secure_save_advice.py
    make_security_examples.py
    fixtures/
      clear_01.png
      synthetic_scanned_pii_3pages.pdf
      synthetic_scanned_pii_ground_truth.json
```

所有命令都從專案根目錄的 PowerShell 執行。建議使用既有 `.venv`、RapidOCR cpu4、PII 快取與中文 NER 單次共用。`pymupdf` 和 `Pillow` 為必要套件；可先執行 `pip show pymupdf pillow` 確認。

```powershell
$env:PII_OCR_BACKEND = "rapid"
$env:PII_NER_BACKEND = "ac"
$env:PII_C_LONG_MODE = "chunk"
$env:PII_MEDIA_CACHE = "1"
$env:PII_ZH_SINGLE_PASS = "1"
```

## 1. 實際 UI 端到端測速：按下按鈕到輸出清單完成

```powershell
.\.venv\Scripts\python.exe .\PII_UI_PDF_Security_Kit\launch_ui_timed.py --project .
```

這個指令會啟動**現有的 UI**。等待預熱完成、右下角狀態回到「就緒」後：

1. 在「檔案」頁面只加入 `PII_UI_PDF_Security_Kit\fixtures\clear_01.png`，按「開始處理」，記錄一次；對同一張圖片至少測試三次。
2. 將清單改成只有 `PII_UI_PDF_Security_Kit\fixtures\synthetic_scanned_pii_3pages.pdf`，再測試至少三次。
3. 不要在同一次點擊中把 PNG 和 PDF 混在一起，否則兩種檔案的總時間難以分開比較。
4. 退出 UI 後執行：

```powershell
.\.venv\Scripts\python.exe .\PII_UI_PDF_Security_Kit\summarize_timing.py
```

輸出：
- `PII_UI_PDF_Security_Kit/results/ui_timing.jsonl`：逐次操作的耗時；**不儲存檔案名稱或原始 OCR 文字**。
- `PII_UI_PDF_Security_Kit/results/ui_timing_summary.md`：每種檔案的中位耗時。

`click_to_ui_s` = 使用者按下「開始處理」至 UI 完成輸出清單更新的時間（含背景工作、佇列等待、UI 更新）。
`worker_s` = 背景工作 `core.dispatch_files` 的耗時；它會寫檔與呼叫既有敏感清單邏輯。
`phases.ocr.seconds` / `phases.pii.seconds` = 累積 `ocr_words` / `find_sensitive_boxes_in_words` 的呼叫時間。

**重要：階段時間存在巢狀呼叫，不能把所有數字相加。** UI 預熱時間不計入這個點擊測試。原本 UI 及 `handler_media` 的 log 仍可能包含個資；只對**合成測試資料**使用本工具。

如需比較「完全不快取」和「已優化」的 UI 端到端時間，可先關閉 UI，分別在不同 PowerShell 工作階段設定 `PII_MEDIA_CACHE=0 / PII_ZH_SINGLE_PASS=0` 與 `1 / 1` 後啟動，並指定不同 `--report` 路徑；不要邊運行 UI 邊變更環境變數。

## 2. 用真實 RapidOCR + PII 模型重跑掃描 PDF 並稽核

```powershell
.\.venv\Scripts\python.exe .\PII_UI_PDF_Security_Kit\run_pdf_regression.py --project . --engine rapid
```

這會呼叫你目前的 `handler_media.mask_pdf()`，處理內附三頁合成掃描 PDF、寫出 PDF，然後使用 `audit_pdf_security.py` 檢查。合成對照表中的原始裁切會在完成後被刪除。

輸出位置：
- `.../results/real_pipeline/synthetic_scanned_masked_rapid.pdf`
- `.../results/real_pipeline/pdf_security_real_rapid.json`

若要增加從 PDF **底層圖片 XObject 抽取圖片後再次 OCR** 的最佳努力檢查（會明顯增加耗時）：

```powershell
.\.venv\Scripts\python.exe .\PII_UI_PDF_Security_Kit\run_pdf_regression.py --project . --engine rapid --ocr-images
```

如果程式報 `FAIL`，不要散布該 PDF；先查出是哪個 `code` 失敗。如果狀態為 `REVIEW`，表示自動檢查尚不能解除所有風險，**不是成功安全認證**。

## 3. 獨立稽核 UI 產生的掃描 PDF

先在 UI 的「檔案」分頁處理合成的三頁 PDF。你的 UI 通常會把輸出儲存在原始檔旁，名稱為 `_masked.pdf`。確認實際輸出路徑後執行：

```powershell
.\.venv\Scripts\python.exe .\PII_UI_PDF_Security_Kit\audit_pdf_security.py `
  --source .\PII_UI_PDF_Security_Kit\fixtures\synthetic_scanned_pii_3pages.pdf `
  --output .\PII_UI_PDF_Security_Kit\fixtures\synthetic_scanned_pii_3pages_masked.pdf `
  --ground-truth .\PII_UI_PDF_Security_Kit\fixtures\synthetic_scanned_pii_ground_truth.json `
  --report .\PII_UI_PDF_Security_Kit\results\ui_pdf_security.json
```

報告的檢查範圍：
- 頁數、已知敏感文字在 PDF 的可抽取文字層與可解碼物件中是否仍出現；
- 即使是**不被頁面引用的圖片物件**，是否仍能抽出與原始 PDF 完全相同的圖片像素；
- 原始位元組、嵌入附件、註解物件、歷史增量修訂、合成標註區域的黑色覆蓋；
- 可選擇額外抽出 PDF 底層圖片做 OCR（`--ocr-images`），但 OCR 漏判仍可能發生。

**請勿直接拿這份合成 Ground Truth 比對真實文件！** 真實文件如無可靠標註，需要先人工確認原文與實際遮蔽位置；自動安全驗證只能列為輔助，不可宣稱「不可能恢復」。如需查真實敏感字串，可透過 `--secret` 指定，但這會留在 PowerShell 命令歷史紀錄，因此不建議用於真實個資。

## 4. 故意不安全的 PDF：驗證稽核真的能抓出錯誤

```powershell
.\.venv\Scripts\python.exe .\PII_UI_PDF_Security_Kit\make_security_examples.py
```

這會產生：
- `BAD_visual_overlay_only.pdf`：畫面黑框已遮住 11 個 PII，但底層**原始圖片仍可抽取**，稽核應輸出 `FAIL: ORIGINAL_IMAGE_RECOVERABLE`。
- `GOOD_known_boxes_redaction.pdf`：使用實際 PDF redaction + `garbage=4`，稽核不應找到完全相同的原圖（但仍可能輸出 `REVIEW`）。

內附 `fixtures/security_demo_preview.png` 可直觀看到兩種情況的外觀幾乎相同，但安全意義不同。

## 5. PDF 儲存設定檢查

```powershell
.\.venv\Scripts\python.exe .\PII_UI_PDF_Security_Kit\secure_save_advice.py --project .
```

如果顯示 `doc.save(output_path)` 未設定垃圾回收，建議在 `mask_pdf` 中**對新輸出檔**使用：

```python
doc.save(output_path, garbage=4, deflate=True)
```

這**僅降低未引用舊物件殘留的風險，不會把黑色覆蓋層變成真正 redaction**。真正刪除 PDF 底層內容仍需先正確使用 `page.add_redact_annot()` 與 `page.apply_redactions()`，並驗證遮蔽區域。請勿直接用這行覆寫既有來源 PDF。

## 5.1 如果 UI 圖片遮蔽「沒有輸出檔」

已確認 GitHub 上的部分 `handler_media.mask_image()` 版本最後沒有 `return entries`，而 UI 的 `core.dispatch_files()` 需要回傳 entries 才會保留圖片輸出。**先讀取檢查，不會修改任何程式：**

```powershell
.\.venv\Scripts\python.exe .\PII_UI_PDF_Security_Kit\check_image_return.py --project .
```

如果顯示需要修正，關閉 UI，再執行：

```powershell
.\.venv\Scripts\python.exe .\PII_UI_PDF_Security_Kit\check_image_return.py --project . --apply
```

工具只會在 `mask_image()` 最後增加 `return entries`，並備份原始檔案；如果函式結構無法安全判斷就拒絕修改。若檢查結果為 `OK`，**完全不需要操作**。

## 6. 測試工具本身

```powershell
.\.venv\Scripts\python.exe -m unittest discover `
  -s .\PII_UI_PDF_Security_Kit -p "test_*.py" -v
```

工具作者已在獨立測試環境跑過 10 項，包含不安全覆蓋版、真 redaction、文字層殘留、報告不包含敏感字串、來源 PDF 不變、UI 計時事件。

## 7. 通過條件與人臉遮蔽下一階段

本階段驗收時請確認：
1. UI 正常回傳輸出檔，且有 `click_to_ui_s` / `worker_s` 的多次測量值。
2. 先前 3 頁合成掃描 PDF 的 11 個已知 PII 仍能被遮蔽。
3. 安全稽核沒有 `FAIL`；若只有 `REVIEW`，逐項說明限制、檢查附件／未測項目，不能當作完全安全。
4. 原始 PDF 未被修改，輸出的 `media_test_map` / `media_deid_map.json` **不得公開上傳或連同遮蔽檔分享**。它們本來就包含可還原的個資原始裁切。

驗收後才進行人臉偵測與遮蔽：先加上獨立 `face_detector.py`（僅本機模型，回傳人臉框），再將人臉框和現有文字 PII 框整合，提供 `PII_FACE_MASK=1` 開關。人臉偵測須增加假陽性／假陰性測試，並保留圖片與 PDF 的原始物件刪除檢查。**先不要用沒有經過驗證的臉部模型直接處理真實敏感文件。**
