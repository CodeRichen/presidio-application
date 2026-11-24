# 畢業專題：`ui.py` 真正可執行的介面美化版

## 這是什麼？

這不是 Canva 圖片，而是根據你在 2026-11-21 上傳的 **55 KB `ui.py` 原始碼**編輯成的 `ui_demo.py`。它會在 Windows 開啟真正的 Tkinter GUI，並共用原有 `core.py`、`handler_media.py`、模型、虛擬環境、剪貼簿等既有處理機制。

**正式 `ui.py` 沒有被覆蓋**，而且不修改 `core.py`、模型設定或 PDF 安全流程。測試資料請使用虛構個資；原本 `core.py` 仍可能在媒體例外時繼續下一個檔案，以及在資料夾掃描裡複製未遮蔽原稿。此版純粹美化介面，不能視為修正了那些問題。

## 啟動（PowerShell）

把整個 `PII_UI_Demo_Refresh` 資料夾解壓到 `presidio_app_allen` 專案根目錄，旁邊應該就是 `ui.py`、`core.py` 和 `.venv`。

```powershell
# 1. 靜態檢查（不用載入 AI 模型）
.\.venv\Scripts\python.exe .\PII_UI_Demo_Refresh\test_ui_demo.py

# 2. 開啟真正的美化 GUI（會匯入正式專案依賴、進行原本模型預熱）
.\.venv\Scripts\python.exe .\PII_UI_Demo_Refresh\launch_ui_demo.py
```

如果只想啟動原本 GUI：

```powershell
.\.venv\Scripts\python.exe .\ui.py
```

兩者不會互相覆蓋。首次啟動模型預熱時間仍取決於電腦及本機模型，UI 美化不會讓 OCR 或 PII 處理變快。

## 美化重點

1. 深藍品牌標題列和「本機處理・畢業專題展示版」標記。
2. 新增「總覽」首頁，展示四大功能和可點擊的快捷入口。
3. 將文字、檔案／PDF、資料夾掃描、名單管理、快速鍵、對照表各自加上標題和說明。
4. 統一 ttk 按鈕、表格、輸入框與分頁配色，主要操作改為藍色強調。
5. 調整清單和文字框大小，確保操作按鈕、執行紀錄和底部狀態列能顯示。
6. 保留原本背景工作 `_start()`、`_poll()`、`WORK_LOCK`、`run_files()` 及各處理函式不變。
7. 不額外安裝 `ttkbootstrap`、`customtkinter` 或其他套件；使用 Python 內建 Tkinter。

## 檔案說明

- `launch_ui_demo.py`：正確設定專案匯入路徑後啟動展示 GUI。
- `ui_demo.py`：真正可執行的介面美化原始碼。
- `ui_original_reference.txt`：用於差異比對的舊 UI 原始碼文字檔。
- `ui_beautify.patch`：原版與美化版差異。
- `test_ui_demo.py`：原業務方法 AST 相同性與基本安全開關靜態測試。
- `preview_overview.png`、`preview_files.png`、`preview_text.png`：在 Linux/Xvfb 使用**模擬依賴**擷取的 UI 畫面，僅供外觀預覽。

## 發表前檢查

請用自己的 Windows `.venv` 和固定的合成文件實際試一次文字、圖片和一頁 PDF。測試時**勿使用真人個資**，也不要在評審前展示私人還原對照表或敏感名單。等功能確認無誤，再以這版錄製 Demo。

**驗證界線：** 已用模擬依賴完成 Linux Xvfb 的 UI 開啟與三個主畫面目視檢查、靜態方法回歸測試。尚未在你的 Windows 電腦上實際運行完整 `core.py`、OCR、PII 模型與真實 PDF；未宣稱正式安全驗收。
