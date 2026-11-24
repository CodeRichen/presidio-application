美化版 Demo UI：修復「安裝成功但看不到還原按鈕」
==================================================
原因：上一版在 App 初始化後動態找分頁，將按鈕用 pack(side="bottom") 放在有兩個 expand 清單的分頁底部，可能被擠出顯示範圍。

本版直接修改使用者上傳的 ui_demo.py：
1. 「檔案 / PDF」上方工具列、開始處理檔案左側加入「還原 PDF／圖片…」按鈕。
2. 還原工作使用原有 App._start / WORK_LOCK 背景執行與忙碌狀態。
3. 先驗證 media map，確認要還原後才建立私有輸出。
4. 以同檔案系統 os.link 安全發布，拒絕覆寫已存在檔案。
5. 拿掉上一版延後建立動態按鈕的 hook，不變更 core.py、handler_media.py。

安裝前條件：已安裝前一版 seamless restore，且專案根目錄存在 seamless_pdf_restore.py，
handler_media.py 的 restore_pdf 已呼叫 restore_pdf_flat。

1. 將 PII_Demo_Visible_Restore_Fix/ 放到 presidio_app_allen/ 專案根目錄。
2. 關閉正在運作的 Demo UI。
3. 在 Windows PowerShell 專案根目錄：
   .\.venv\Scripts\python.exe .\PII_Demo_Visible_Restore_Fix\install_visible_restore_button.py --check
   預期 STATUS: CHECK_PASS (read only)
4. 確認符合再執行：
   .\.venv\Scripts\python.exe .\PII_Demo_Visible_Restore_Fix\install_visible_restore_button.py --apply
   預期 STATUS: PATCH_INSTALLED
5. 確認語法：
   .\.venv\Scripts\python.exe -m py_compile .\PII_UI_Demo_Refresh\ui_demo.py
6. 啟動：
   .\.venv\Scripts\python.exe .\PII_UI_Demo_Refresh\launch_ui_demo.py

按鈕位置：「檔案 / PDF」分頁上方，位於「開始處理檔案」左邊。
若選取一筆輸出清單，按還原會優先使用該檔案；否則彈出選檔視窗。
程式關閉會清除還原對照表，請在同一次執行階段內完成測試。
還原後包含原始人臉／敏感內容，僅可於私有環境保存，不可公開分享。

注意：此安裝包只修改 ui_demo.py；保留備份：ui_demo.py.backup_<時間戳>。
如果校驗不符會 REFUSED，不會強制覆蓋更新中的介面。
