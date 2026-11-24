# V6.6.8.1｜隔離 GUI「單模式」卡頓診斷

這份工具要釐清 V6.6.8 隔離 Tkinter A/B 中，觀測到的 **965.261 ms 最大額外排程延遲**（p95 約 10.861 ms）。前一輪按一次按鈕連續跑了 GPU 與 CPU 兩組完整 PDF，因此 **319.442 s 不是單次 PDF 的 GUI 等待時間**。本版把模式分開，以便觀察與比較。

**這不是正式 `ui.py`／`core.py` 的整合版本，也不允許測真實文件。** 工具不修改 `ui.py`、`core.py`、`handler_media.py`、`ocr_backend.py`、模型、`.venv`、`.venv_ocr_gpu`；亦不自行更改辨識規則。PDF 只使用固定七頁合成來源，並借用 V6.6.7.6 已有的 SHA-256 檢查、安全驗收及實驗 GPU worker。

## 這次量什麼

三個獨立按鈕（**每按一次只做一種模式**）：

| 模式 | 工作內容 | 主要用途 |
|---|---|---|
| 混合 GPU | 固定合成七頁 PDF、2×+3× OCR，det CUDA／cls+rec CPU；完整 PII／人臉／V5 PDF 結構安全 Gate | 觀察單次混合模式的介面延遲及等待時間 |
| 原始 CPU | 相同合成 PDF 與安全 Gate，全 CPU OCR | 觀察單次 CPU 模式的介面延遲及等待時間 |
| GUI 閒置 | 不執行 OCR／PDF，僅 Tkinter `after(100ms)` 心跳 30 秒（可調） | 判斷是否即使無推論負載也有排程尖峰 |

每次在 Tkinter 事件迴圈中記錄排程超出 100 ms 週期的額外延遲（延遲不是 GUI 阻塞時間的精確量測）。記錄最大值、p50、p95、p99、超過 100／250／500／1000 ms 的次數，以及最多 12 筆尖峰的相對時間和**粗略觀測階段**。標記的階段（如 `GPU_FULL_PDF`）**不等於根因**，不能直接說卡頓是 CUDA 初始化造成的。PDF 處理會在背景執行緒啟動子程序，UI 本身不做同步 OCR。

每個非閒置模式還會列出 `processing_s`、`run_and_audit_s`、`child_elapsed_s`、以及點擊到隔離 GUI 完成回呼開始時的時間 `click_to_finish_s`。**三種時間不可互換**。混合組另包含首頁 OCR Adapter 2×／3× 預檢、跨程序通訊、模型啟動等時間，這不是正式 App `ui.py` 的量測。

本輪**沒有執行 CPU／混合輸出像素互相比對**，因此不能單憑本輪說兩者像素完全一致；此項已在 V6.6.7.6 三組完整 A/B 獨立驗證過。

## 前置條件

1. 在 Windows 的 `presidio_app_allen` 專案根目錄操作。`PII_V6681_GUI_Lag_Diagnostic/` 與 `PII_V6676_Full_PDF_Hybrid_AB/` 皆放在根目錄下（不能巢狀）。
2. 原本的 `.venv/Scripts/python.exe`、`.venv_ocr_gpu/Scripts/python.exe` 和 `PII_V5_Security_Acceptance/` 完整保留。不要改動任何程式或套件。
3. 固定七頁合成 PDF 檔案：`PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf`。工具會驗證 SHA-256 `df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49`；不相符即拒絕執行，不要用其他 PDF 替代。
4. **關閉正式 GUI**、其他 GPU 訓練／高負載程式。測試時保持供電模式與前一輪一致。不要讓多個測試 GUI 同時運作。
5. 這是離線測試工具；不使用網路 API，不需要新安裝任何 Python 套件。

## PowerShell 操作

在 `presidio_app_allen` 根目錄開啟 PowerShell。

### 1. 測試本工具邏輯（不啟動 OCR 模型）

```powershell
.\.venv\Scripts\python.exe .\PII_V6681_GUI_Lag_Diagnostic\test_v6681.py
```

### 2. 檢查固定資料、V5 工具與兩套環境

```powershell
.\.venv\Scripts\python.exe .\PII_V6681_GUI_Lag_Diagnostic\v6681_gui.py --check-only
```

應顯示 `CHECK_OK`。若失敗，先釐清原來的檔案／環境，**不要自行略過 SHA256 或安全 Gate**。

### 3. 啟動單模式測試 GUI

```powershell
.\.venv\Scripts\python.exe .\PII_V6681_GUI_Lag_Diagnostic\v6681_gui.py `
  --results-dir .\v6681_gui_results `
  --idle-seconds 30
```

建議在同一個視窗中分別各按一次：**無負載觀察 30 秒、只測混合 GPU PDF、只測 CPU PDF**。每次執行完畢，才點下一個按鈕。也可以重新啟動 GUI 做額外輪次；報告採 `v6681_run_01`, `02`, `03`… 遞增編號，不覆寫舊檔。

若需更多閒置樣本，可把 `--idle-seconds 30` 改成 `60`，不需要修改程式。子程序單次超時預設 1800 秒，如遇到異常應先檢查報告的失敗狀態，而不是繞過驗收。

## 取得結果並上傳

```text
presidio_app_allen/
  v6681_gui_results/
    v6681_run_01.json
    v6681_run_01.md
    v6681_run_02.json
    v6681_run_02.md
    v6681_run_03.json
    v6681_run_03.md
```

請上傳**三種模式各一組的 JSON 和 MD**，亦即六個檔案。可執行以下 PowerShell 先看摘要：

```powershell
Get-ChildItem .\v6681_gui_results\v6681_run_*.json | ForEach-Object {
    $r = Get-Content $_.FullName -Raw | ConvertFrom-Json
    [PSCustomObject]@{
        Mode = $r.mode
        Status = $r.status
        Seconds = $r.gui.click_to_finish_s
        MaxLagMs = $r.gui.max_lag_ms
        P99LagMs = $r.gui.p99_lag_ms
        Over500 = $r.gui.lag_count_above_ms.'500'
    }
} | Format-Table -AutoSize
```

結果解讀：

- `SINGLE_MODE_SYNTHETIC_SAFETY_PASS`：該模式固定合成 PDF 的 V5 Gate 通過，**不是正式 GUI 部署完成**；本輪未做 CPU／GPU PDF 像素互比。
- `IDLE_GUI_MEASURED`：完成 Tkinter 閒置心跳量測，不涉及 PDF 安全性。
- `SINGLE_MODE_EXECUTION_OR_SAFETY_FAILED`：任一子程序、固定合成 PDF 或安全 Gate 未通過；不可採納該輪加速結論。
- `GUI_SCHEDULING_REVIEW`：觀測到排程額外延遲超過 500 ms，仍需確認出現時機；沒有此標記也不等於已證明 UI 完全不卡。

## 隱私與安全注意

只在暫存目錄執行固定七頁合成 PDF 的完整安全 Gate；不輸出 OCR 原文、文字雜湊、圖片、字框、遮蔽 PDF、私有 map、原始 ORT profile、檔案路徑或 traceback。結果 JSON／MD 只保存預先指定的狀態、時間、有限模型 Node 計數、階段和統計。正常結束會清理產物；若強制中止，請檢查系統 `%TEMP%` 中 `pii_v6681_*`、`pii_v6675_worker_*` 前綴的暫存資料夾。

完成 V6.6.8.1 的單模式觀測後，若要真正接到正式 GUI，需要對照你**當前版本**的 `ui.py`、`core.py`；不可拿舊版參考檔直接覆蓋。

## V6.6.8.1 Windows 短路徑單元測試修正

修正 `test_destination_inside` 的 expected path，先呼叫 `.resolve()` 才和
`validate_destination()` 回傳的已解析絕對路徑比對。

Windows `%TEMP%` 有時以 `C:\CONDA_~1` 等 8.3 短路徑表示，`Path.resolve()`
可能回傳 `C:\conda_tmp` 等對應長路徑；原測試因兩種名稱而誤判 FAIL。

**僅修改 `test_v6681.py` 的斷言；`v6681_gui.py` 完全未修改。**
不放寬輸出目錄檢查，禁止真實文件，原本的安全測試限制維持不變。
