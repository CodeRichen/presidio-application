# V6.6.8：隔離 Tkinter GUI 與混合 OCR 完整 PDF 安全 A/B

> **重要範圍說明：這是獨立的測試 GUI，並非現有 `ui.py` / `core.py` 的整合版本。**
> 它不能用來宣稱正式 GUI 已完成混合 OCR 整合或正式應用可處理真實文件。

## 已知基準

V6.6.7.6 已在固定七頁**合成** PDF 上完成 3 組交錯 A/B，純處理時間中位數 CPU 180.849 s、混合 77.967 s（改善 56.89%）；每組 27/27 固定文字框、17/17 已記錄人臉框、PDF 安全結構及 7 頁像素比對均通過。該結果不包含正式 GUI 的點擊與繪圖時間。

## 這一輪解決什麼

使用一個獨立 Tkinter 視窗，點擊按鈕後以背景執行緒跑 V6.6.7.6 原有的完整合成 PDF 安全測試：

1. 先執行混合模式（GPU det、CPU cls/rec；從正式 `.venv` 呼叫獨立 `.venv_ocr_gpu` worker）。如果 OCR Adapter、PII、人臉、PDF 結構或硬體路由失敗，就立即停止，不跑 CPU。
2. 執行正式 CPU 基準模式。
3. 比較兩份 PDF 的全部七頁渲染像素；在暫存目錄刪除兩份測試 PDF 與測試中間檔。
4. 測量 GUI 按鈕到測試完成回呼的時間、Tkinter `after(100)` 排程額外延遲最大值與 p95，保留安全統計 JSON/MD。

**注意：按鈕到完成的時間包括連續執行兩套 CPU/GPU 流程和安全稽核，不是處理單份 PDF 的使用者等待時間。** 這個隔離 GUI 在目前環境不能代替正式 GUI 的 UX 卡頓測試。

## 安裝條件

在 Windows 的 `presidio_app_allen` 根目錄，**保留**：

- `.venv/Scripts/python.exe`（正式 CPU/PII/V5）
- `.venv_ocr_gpu/Scripts/python.exe`（隔離 ONNX GPU）
- `PII_V6676_Full_PDF_Hybrid_AB`（完整解壓，含原始 `v6676_benchmark.py`, `v6676_child.py`, worker 和 bridge）
- `PII_V5_Security_Acceptance`、`PII_Face_PDF_7page_Test`、正式 `ocr_backend.py`、`handler_media.py` 及其正常依賴
- 新解壓的 `PII_V668_Isolated_GUI_Smoke`

**不要使用**真實 PDF、私密資料或自行指定另一個輸入檔。工具內沒有選檔功能，只允許固定 SHA-256 `df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49` 的合成七頁 PDF。V6.6.7.6 會再檢查這個 SHA。

## 執行（PowerShell，在專案根目錄）

```powershell
# 1. 本機單元測試（只用標準函式庫，不執行 OCR）
.\.venv\Scripts\python.exe .\PII_V668_Isolated_GUI_Smoke\test_v668.py

# 2. 確認合成檔、V5、安全 A/B、兩套虛擬環境都存在
.\.venv\Scripts\python.exe .\PII_V668_Isolated_GUI_Smoke\v668_gui.py --check-only

# 3. 開啟獨立測試視窗
.\.venv\Scripts\python.exe .\PII_V668_Isolated_GUI_Smoke\v668_gui.py `
    --results-dir .\v668_gui_smoke_results
```

點選「**執行一組 CPU／混合 GPU A/B**」一次即可。只要介面狀態為忙碌，就不要強制關閉 GUI 或結束終端機，避免遺留 OS 暫存目錄的合成測試產物。長時間異常時仍可終止測試，但請檢查 `%TEMP%` 中 `pii_v668_` 開頭的暫存目錄及 GPU worker 是否已停止。

## 上傳的報告

```text
v668_gui_smoke_results/
  v668_gui_smoke_summary.json
  v668_gui_smoke_summary.md
```

僅輸出安全指標與模型 Node 事件計數，不儲存 OCR 原文、文字雜湊、字框、私人 map、圖片、輸出 PDF、原始 profiler 或原始子程序 stderr。成功時狀態為 `ISOLATED_GUI_AB_SAFE`；介面排程延遲可能另提示 `GUI_SCHEDULING_REVIEW`（超過 500 ms），且要獨立解讀。

## 之後真正的正式 GUI 整合

本次只驗證「GUI 事件迴圈＋V6.6.7.6 安全 A/B」可在隔離視窗下運作。接著仍必須提供**最新版** `ui.py` 和 `core.py`，以便對目前的 `core.dispatch_files(paths, mapping)` 工作流做不覆蓋正式版的實驗性整合；在完整 GUI 輸出安全驗收、冷暖啟動、多檔案和故障回退驗證前，不應啟用真實文件混合 GPU 模式。
