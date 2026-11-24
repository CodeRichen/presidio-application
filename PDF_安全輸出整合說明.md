# PDF 安全輸出修正（適用於 `handler_media.py`）

## 修改方式

把 `pdf_secure_output.py` 放在與 `handler_media.py` 相同的資料夾。

在 `handler_media.py` import 區新增：

```python
from pdf_secure_output import save_secure_raster_pdf
```

**PDF 遮蔽工作完成後**，原本的：

```python
doc.save(output_path)
```

替換成：

```python
save_secure_raster_pdf(doc, output_path, dpi=200)
```

建議把開啟 PDF、逐頁遮蔽和安全輸出放在 `try/finally` 或 `with` 結構中，確保失敗時關閉檔案。
只有在安全輸出成功後，才呼叫既有的 `_save_map_entry` 儲存還原對照表。UI `mask_file()` 入口無須變更。

## 本機快速測試

在已啟用 `.venv` 的專案資料夾執行：

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_pdf_secure_output.py
```

接著以原先的 3 頁合成 PDF 跑完整的 OCR+PII→mask_pdf 流程，再執行既有的 PDF 安全回歸測試。

## 安全特性及限制

- 安全輸出會建立新 PDF，**不會**沿用原 PDF 的附件、註解、PDF 文字層、表單、隱藏圖層、連結、JavaScript、metadata 等物件。
- 頁面會影像化，無法選取/搜尋原本的文字，檔案可能增大且需花額外渲染時間。
- 對於有旋轉或裁切的 PDF，必須先處理舊版還原對照表的座標轉換；對於一般未旋轉的掃描 PDF，頁面尺寸與 bbox 座標維持一致。
- 僅確保輸出採用遮蔽後可見像素，不保證 OCR/PII 沒漏抓。敏感內容如果根本沒有被畫黑，依然可能出現在新 PDF。
- 既有 `clipboard_map.txt` 或 PDF 還原對照表中可能保存原始敏感片段，**不可**隨輸出 PDF 一起分享；需另行安全存放及控管存取。
- 不會修改來源 PDF。
