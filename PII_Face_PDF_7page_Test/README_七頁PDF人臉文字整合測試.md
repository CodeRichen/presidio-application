# 七頁掃描 PDF：文字 PII + 離線人臉遮蔽整合測試

## 這份測試包含什麼？

- `pii_face_7page_scan_source.pdf`：七張「尚未遮蔽」的合成測試圖，各占 PDF 一頁；**頁面只有影像，沒有文字層**。
- 來源 PDF **刻意包含一個合成的文字附件** `synthetic_attachment_canary.txt`，用來測試 `pdf_secure_output.py` 是否真的移除來源附件。**輸出的遮蔽 PDF 必須有零個附件**。
- `expected_text_and_faces.json`：七頁合計 27 個已知文字 PII（姓名、電話、Email）的標註座標；人臉數量是目視估計，不是已驗證的 bounding-box 真值。
- `run_7page_pdf_test.py`：呼叫專案現有 `handler_media.mask_pdf()` 並接著安全與覆蓋率驗證。
- `audit_7page_pdf.py`：對已遮蔽的 PDF 做結構與視覺覆蓋率檢查，不做 OCR 推論。
- `create_7page_pdf.py`：需要時，可以從原本七張 PNG 重新產生來源 PDF。

## 安裝

將此壓縮包**解壓到** `presidio_app_allen/PII_Face_PDF_7page_Test/`。不需要替換你的 `handler_media.py`、`face_detector.py` 或 `pdf_secure_output.py`。

先確認使用的專案已安裝原本的依賴套件（PyMuPDF、Pillow、numpy、RapidOCR、Presidio、YuNet ONNX 等），且先前「7 張 PNG」測試成功。

## 一鍵執行（PowerShell；請在專案根目錄執行）

```powershell
$env:PII_OCR_BACKEND="rapid"
$env:PII_NER_BACKEND="ac"
$env:PII_FACE_ENABLED="1"

.\.venv\Scripts\python.exe .\PII_Face_PDF_7page_Test\run_7page_pdf_test.py `
  --source .\PII_Face_PDF_7page_Test\pii_face_7page_scan_source.pdf `
  --expected .\PII_Face_PDF_7page_Test\expected_text_and_faces.json `
  --results-dir .\pdf_face_7page_results
```

首次執行如果已存在相同輸出，程式會拒絕覆寫。確認要重跑時，請加上 `--overwrite`。

`run_7page_pdf_test.py` 只在它所啟動的 Python 行程內啟用 `PII_MEDIA_FP_PROFILE=synthetic_face_test`，供這份**合成測試資料**使用；不會永久改變你 PowerShell 的設定。真實資料不應啟用這組白名單。

## 輸出

```text
pdf_face_7page_results/
├── pii_face_7page_masked.pdf              # 需要目視檢查的去識別化結果
├── pdf_face_7page_audit.json              # 可貼給 ChatGPT 的安全摘要
└── PRIVATE_restore_map_DO_NOT_SHARE.json  # 包含原始人臉和文字裁圖，絕對不要上傳！
```

注意：來源 `pii_face_7page_scan_source.pdf` 本身**沒有**遮蔽，不能當作公開安全輸出。

## 驗收項目

1. **完整性**：輸出頁數為 7；每頁文字 PII 遮蔽範圍需通過至少 98% 的黑色像素覆蓋率（27 個項目）。
2. **PDF 結構**：來源有一個合成附件，輸出應為 0 個附件；輸出沒有可抽取文字、註解，並且只有一個 EOF 結尾。
3. **人臉紀錄**：從私有 map 找到各頁 FACE 框的數量，確認這些框在結果 PDF 內是黑色；與目視估計的人臉數量比較，若較少應調查漏判。
4. **視覺驗收**：一定要逐頁看所有人臉（包含側臉、小臉、遮擋）是否真的全部遮住，不能只憑 FACE 份數或自動化 PASS 判定。
5. **誤遮蔽**：留意 `PII + FACE`、`合成資料` 等標題是否被誤遮，以及人臉以外是否有過大的黑框。
6. **效能**：記錄 `PROCESSING_TIME_SECONDS`，後續再與相同環境下其他批次測試比較。

`additional_black_pixels_panel` 只是右側資料面板 Ground Truth 以外的額外變黑像素，包含合理 Padding，**不能直接視為誤判個數**。

## 如果你從 UI 另外產出了 PDF

可以跳過整合腳本，直接對 UI 的輸出執行稽核：

```powershell
.\.venv\Scripts\python.exe .\PII_Face_PDF_7page_Test\audit_7page_pdf.py `
  --source .\PII_Face_PDF_7page_Test\pii_face_7page_scan_source.pdf `
  --output "你的遮蔽結果.pdf" `
  --expected .\PII_Face_PDF_7page_Test\expected_text_and_faces.json `
  --report .\pdf_face_7page_results\ui_pdf_audit.json
```

如果有私有 map 且檔名正確，可加 `--map-file "私有對照表位置"`；無 map 的情況無法自動檢查 FACE bbox。

## 限制

- 這只是針對已知合成測試文件的回歸測試，不保證所有個資都無法恢復；PDF 中的 OCR 漏判、隱藏圖片、複雜頁面等仍需更廣泛測試。
- 人臉數量是目視估計，沒有全部臉的精確人工標註框，**不能拿此報告計算 YuNet 的正式 Recall/F1**。
- 某些 PDF 圖像重取樣後，遮蔽邊界可能有少量非全黑像素；若未達 98% 門檻，必須檢查是否真正露出可辨識文字。
- 不應將對照表與公開的去識別化 PDF 放在同一個共享資料夾，也不要將包含還原裁圖的檔案提交至 Git。
