# V6.6.9.2：在正式 `ui.App` 事件迴圈中驗證混合 GPU OCR（合成 PDF 專用）

本工具是**隔離的測試啟動器**，不是正式 GPU OCR 上線補丁。它會 import 你電腦上現有的 `ui.py`，在記憶體內額外安裝「V6.6.9.2 合成測試」分頁；原本的文字、檔案、資料夾掃描、名單管理等分頁全部停用。`ui.py`、`core.py`、`handler_media.py`、`ocr_backend.py` 及兩套 `.venv` 皆不修改。

## 目的

- 確認真正的 `ui.App._start()` 背景工作、`WORK_LOCK`、`self.q` 與 `_poll()` 完成回呼，在混合 GPU 子程序執行時正常運作。
- 三種模式：**混合 GPU 單模式**、**CPU 單模式**、**CPU/GPU 安全 A/B + 七頁逐像素比對**。
- 單模式的按鈕至回呼耗時和心跳延遲（P95、P99、最大值、超過 500ms 次數）更接近實際 GUI 流程，但仍包含獨立測試預檢及安全稽核。
- 重用已驗證的 `PII_V6676_Full_PDF_Hybrid_AB/v6676_benchmark.py`，完整保留 **2× + 3× OCR**、27/27 固定文字 GT、17/17 已記錄人臉框、PDF 結構安全驗收。
- A/B 模式才進行 CPU/GPU **七頁逐像素**比較；單模式**不能**宣稱通過本輪 CPU/GPU 像素相等測試。

## 強制安全限制

1. 只允許固定 SHA-256 `df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49` 的 `PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf`。工具**沒有**外部 PDF 選取參數。
2. 測試時所有原 GUI 分頁都被停用，`run_files`、`run_scan`、`run_text` 在**此啟動器程序內**再額外覆蓋為無效操作；不得用此實驗版處理真實文件。
3. 預檢檢查固定合成 PDF、AST 原始碼掛接點、V6676/V5 安全工具及隔離 CPU/GPU Python 環境。
4. `v6676_benchmark.run_trial()` 子程序的合成測試專用 FP 設定 **只作用於子程序**，不得複製成正式真實文件設定。
5. 任何 OCR Adapter、CUDA 路由、PII、YuNet 或 PDF 結構不通過，狀態為失敗；A/B 比像素不同也失敗。
6. 僅統計報告保存於指定 `v6692_gui_results` 目錄；PDF、OCR 中間資料及 V5 audit 只存於 `TemporaryDirectory`，一般完成時自動清理。程序強制中止時仍可能有殘留，請檢查 `%TEMP%` 中以 `pii_v6692_` 開頭的暫存目錄。
7. 不驗證正式 GUI 的真實文件處理流程；正式 `core._mask_media()` 既有的失敗時複製原檔和 `dispatch_files()` 忽略媒體處理例外問題仍待獨立安全修正。

## 操作（PowerShell；在 `presidio_app_allen` 根目錄）

將 ZIP 解壓後，應出現 `PII_V6692_Official_GUI_Synthetic_Integration` 目錄。**不要覆蓋現有 `ui.py` 或 `core.py`**。先完全關閉其他已開啟的 GUI。

```powershell
.\.venv\Scripts\python.exe .\PII_V6692_Official_GUI_Synthetic_Integration\test_v6692.py

.\.venv\Scripts\python.exe .\PII_V6692_Official_GUI_Synthetic_Integration\v6692_gui.py --check-only

.\.venv\Scripts\python.exe .\PII_V6692_Official_GUI_Synthetic_Integration\v6692_gui.py `
  --results-dir .\v6692_gui_results
```

建議測試順序：先選 **混合 GPU 單模式**，再選 **CPU 單模式**，最後選 **CPU/GPU 像素 A/B**。每個按鈕都是獨立工作，執行期間不會接受第二次測試。

完成後上傳 `v6692_gui_results/v6692_run_001.json`、`.md`，依序類推 002、003。若 `--check-only` 顯示 `PREFLIGHT_REVIEW_REQUIRED`，請貼上 `CHECK_FAILED` 項目；不要略過或刪除安全檢查。

## 通過標準及限制

- **`SINGLE_MODE_SYNTHETIC_GATE_PASS`**：只代表該模式固定合成 PDF 的 PII、人臉、PDF 結構、OCR 路由等安全檢查通過。
- **`SYNTHETIC_GUI_PAIR_PASS`**：代表 A/B 兩組都通過，且七頁 PDF 像素完全相同。
- 只有安全檢查及 Tk 心跳報告都觀測正常，才進入下一輪正式 GUI 掛接修正；**不代表已獲准用真實文件**。
- GUI 啟動本身、模型初始化、正式程式接入真實檔案、例外時原稿洩漏保護，仍需獨立驗收。
