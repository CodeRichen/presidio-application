# PDF 無接縫還原 + Demo UI 還原按鈕（保留現有功能）

## 功能

- 僅把 `handler_media.py` 的舊 `restore_pdf()` 換成平面影像還原；不修改 `mask_pdf()`、`mask_image()`、`core.py`、OCR、NER、YuNet。
- 不再在 PDF 上用 `page.insert_image` 疊加多張還原裁圖，而是每頁統一畫布合成後重新輸出單張影像。
- 原先臉/文字還原對照表仍為 `presidio/media_deid_map.json`，必須在 Demo UI 關閉清理對照表前完成還原。
- 新增 `PII_UI_Demo_Refresh/ui_demo.py` 的「還原 PDF／圖片…」入口（檔案頁的按鈕／上方「媒體還原」選單）。選取 `*_masked.pdf` 後選擇新的輸出檔名，拒絕覆寫任何現有檔案。
- 2x 裁圖與 3 像素邊界延伸只針對此專題的「無旋轉、每頁一張整頁影像」輸出，不保證 byte-identical 還原。還原結果包含真實敏感資料，不得公開分享。

## 安裝

把整個 `PII_Seamless_Restore_Integration` 資料夾放在 `presidio_app_allen` 專案根目錄，**保留目前 `handler_media.py`、`ui_demo.py` 原始檔**。先關閉所有 Demo UI / Python GUI。

在專案根目錄 PowerShell：

```powershell
.\.venv\Scripts\python.exe .\PII_Seamless_Restore_Integration\install_restore_integration.py --check
```

預期 `STATUS: CHECK_PASS（未修改檔案）`。如果 `REFUSED`，不要使用 `-Force` 或整檔覆蓋；需提供實際 `PII_UI_Demo_Refresh/ui_demo.py` 以便精準合併。

檢查通過後才安裝：

```powershell
.\.venv\Scripts\python.exe .\PII_Seamless_Restore_Integration\install_restore_integration.py --apply
.\.venv\Scripts\python.exe -m py_compile .\handler_media.py .\PII_UI_Demo_Refresh\ui_demo.py .\seamless_pdf_restore.py .\ui_demo_media_restore.py
.\.venv\Scripts\python.exe .\PII_UI_Demo_Refresh\launch_ui_demo.py
```

已安裝後，程式主要使用：`handler_media.restore_pdf()` -> `seamless_pdf_restore.restore_pdf_flat()`；介面掛接 `ui_demo_media_restore.patch_demo_app(App)`，**不改原本美化版布局程式碼**，只新增使用者還原入口。

## 驗收

1. 在開啟 Demo UI 的同一次執行期間，以新的合成 PDF 名稱做一次遮蔽。
2. 保持 Demo UI 開著；確定 `Test-Path .\presidio\media_deid_map.json` 為 `True`。
3. 「檔案」頁按 **還原 PDF／圖片…**（也可使用視窗上的「媒體還原」選單），選這次 `*_masked.pdf`。
4. 選新的還原輸出名稱、確認私有還原風險。等 UI 顯示完成後，開啟還原 PDF 比對七頁文字／人臉接縫。
5. 檢查原本 `*_masked.pdf` **沒有被更動**；還原 PDF 頁數正確且每頁只有一張影像。
6. 再執行原本 27/27 Ground Truth 及失敗回滾測試，確保安全遮蔽未退步。

## 回滾

安裝後會備份 `handler_media.py.backup_<時間>` 和 `PII_UI_Demo_Refresh/ui_demo.py.backup_<時間>`；停掉 GUI 後再還原備份即可。新增的輔助模組可以另外移除。不要修改、公開或轉存任何包含原始人臉裁切圖的 `media_deid_map.json`。
