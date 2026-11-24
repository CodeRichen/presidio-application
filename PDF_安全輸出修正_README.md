# `handler_media.py` PDF 安全輸出修正

## 修正原因

你上傳的 `handler_media.py` 中 `mask_pdf()` 在執行 OCR、PII 遮蔽與 `page.apply_redactions()` 後，最後採用：

```python
doc.save(output_path, garbage=4, deflate=True)
```

`garbage=4` 並不會自動移除仍被引用的 PDF 附件，因此安全掃描仍偵測到 `ATTACHMENTS_PRESENT`、`UNCHANGED_ATTACHMENTS`。

## 使用方式

在 `presidio_app_allen` 專案根目錄：

1. 備份原本的 `handler_media.py`。
2. 將本包的 `handler_media.py` **替換**原檔；將 `pdf_secure_output.py` 放在同一目錄。
3. 選擇性地將兩個 `test_*.py` 也放在專案根目錄。
4. 不需要更改 main.py / UI 的呼叫方式；既有 `mask_file()`、`restore_file()` 介面不變。

## 修正的核心部分

```python
from pdf_secure_output import save_secure_raster_pdf

# mask_pdf() 裡原先呼叫 doc.save(...) 的位置改成：
save_secure_raster_pdf(doc, output_path, dpi=200)
```

此函式將已遮蔽的可見頁面重新渲染成 PNG，再放入**全新的 PDF 文件**；不複製來源文件的附件、註解、超連結、表單、JavaScript、文字層和原始 image xref。輸出寫入暫存檔、重新開啟檢查，成功後才原子替換目標 PDF。`handler_media.py` 會在成功後再寫入還原對照表。

安全輸出後，正常應看到：

```text
[PDF安全輸出] 已重新建立純影像 PDF，保留原本 OCR / PII 遮蔽流程
[PDF安全檢查] 附件數量：0
```

## 本機測試

```powershell
.\.venv\Scripts\python.exe -m py_compile handler_media.py pdf_secure_output.py
.\.venv\Scripts\python.exe -m unittest -v test_pdf_secure_output.py test_handler_media_secure_integration.py
```

這兩組測試不依賴 Windows OCR / Presidio 模型；整合測試以模擬 OCR 產生的 bbox 進行**真實 PDF 遮蔽及輸出**。上面通過不能取代實際 OCR / PII 精度驗證。

接著對原來的 3 頁合成掃描 PDF **重新產生新的遮蔽 PDF**，再重新跑：

```powershell
.\.venv\Scripts\python.exe .\PII_UI_PDF_Security_Kit\run_pdf_regression.py --project . --engine rapid --ocr-images
```

請確認回歸腳本檢查的是**新產生**的 PDF，原本的測試結果 JSON 不會因為換了程式碼就自行更新。

期望 `ATTACHMENTS_PRESENT` 和 `UNCHANGED_ATTACHMENTS` 不再是 REVIEW；如果仍是 REVIEW，請對照程式顯示的 PDF 輸出路徑、模組載入路徑及回歸腳本的實際輸入路徑。

## 限制與資料保護

- 對於已使用 `/Rotate` 旋轉的來源 PDF，此版本會拒絕處理，避免原本 OCR pixel bbox 與 PDF 座標不一致導致漏遮。請先將來源 PDF 的頁面方向正規化或後續擴充旋轉支援。
- PDF 重建後變成**純影像 PDF**，不能選取原始文字，檔案大小可能增大，輸出階段會額外耗時。
- 遮蔽完的 PDF 會排除原始附件，但如果 OCR 或 PII 漏檢，敏感內容仍可能出現在頁面像素。請繼續執行視覺覆蓋和 OCR 回歸測試。
- 既有 `media_deid_map.json` 含有原始 PII 及原始畫面裁切圖，**不要隨遮蔽 PDF 一起公開**。需要配合資料保存期限、存取控制；如需真正不可還原的對外版本，可停用原始裁切圖對照表。
- `restore_pdf()` 是**授權環境內**用原始裁切圖還原畫面；重新輸出的還原 PDF 會重新包含個資，不應視為已去識別化檔案。
- 目前附帶的模擬測試不保證所有 PDF 變體都絕對不洩漏，必須使用你原先的安全回歸工具和人工檢查確認。
