# 美化 Demo UI：固定顯示「還原 PDF／圖片…」按鈕（適配版）

此版不再要求 `ui_demo.py` SHA256 與先前上傳版本完全一致，而是檢查 App、檔案操作列與工作執行緒錨點是否存在且唯一。只有已整合 `seamless_pdf_restore.py` 後端時才允許安裝。

先將整個 ZIP 解壓在 presidio_app_allen 專案根目錄，並先**完全關閉 Demo UI**。

唯讀檢查：

```powershell
.\.venv\Scripts\python.exe .\PII_Demo_Visible_Restore_Adaptive\install_visible_restore_adaptive.py --check
```

預覽變更（仍唯讀）：

```powershell
.\.venv\Scripts\python.exe .\PII_Demo_Visible_Restore_Adaptive\install_visible_restore_adaptive.py --diff
```

確認 CHECK_PASS 後，備份並套用：

```powershell
.\.venv\Scripts\python.exe .\PII_Demo_Visible_Restore_Adaptive\install_visible_restore_adaptive.py --apply
```

重新啟動：

```powershell
.\.venv\Scripts\python.exe .\PII_UI_Demo_Refresh\launch_ui_demo.py
```

打開「檔案 / PDF」，在「開始處理檔案」左邊應看到「還原 PDF／圖片…」。選取已遮蔽的檔案並確認仍存在私有對照表，才能還原。不要公開還原成果。

如顯示 REFUSED：代表你的實際程式碼結構和這份工具認得的程式不同，**不要硬改或關閉校驗**，提供目前磁碟上原始 `PII_UI_Demo_Refresh\ui_demo.py` 以做精準修改。這份工具只修改 UI，不會觸及 core.py 和 handler_media.py，且拒絕覆寫原有還原檔。
