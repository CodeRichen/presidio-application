# Presidio App：GUI V2 效能瓶頸診斷工具

本工具只觀察階段名稱、呼叫次數、耗時、Tk `after()` 排程延遲，以及程序 RSS。**不覆蓋 `ui.py`、`core.py`、`handler_media.py`，不修改遮蔽結果或還原檔**。

## 一、使用方法（Windows PowerShell）

1. 關閉所有既有 GUI。將整包 ZIP 解壓到 `presidio_app_allen` 專案根目錄，讓路徑為 `presidio_app_allen/PII_GUI_V2_Diagnostics/`。
2. 在專案根目錄執行：

```powershell
.\.venv\Scripts\python.exe .\PII_GUI_V2_Diagnostics\test_v2.py
```

3. 使用合成資料測試模式啟動 GUI：

```powershell
.\.venv\Scripts\python.exe .\PII_GUI_V2_Diagnostics\gui_v2_launcher.py --synthetic-test
```

- **測試資料維持一致**：`7page_pdf_preview.jpg` + `pii_face_7page_scan_source.pdf`，在 GUI「檔案」分頁同時選取兩個，按「開始處理」。
- 建議在同一個 GUI 程序內連續執行 **3 次**，結束後關閉 GUI。
- 想對正式文件診斷：**不要**使用 `--synthetic-test`，並先在 PowerShell 執行 `Remove-Item Env:PII_MEDIA_FP_PROFILE -ErrorAction SilentlyContinue`。注意輸出仍可能保存原始私人裁切圖至原本專案的對照表，請只用合成資料測試。
- **不要同時啟動 V1 Regression Launcher**：兩者都會攔截 GUI 啟動方法。V2 已經包含必要的 UI 延遲與 Worker 耗時監測。

4. 產生彙整報告：

```powershell
.\.venv\Scripts\python.exe .\PII_GUI_V2_Diagnostics\summarize_v2.py --results-dir .\gui_v2_results
```

5. 上傳 `gui_v2_results/gui_v2_summary.json` 與 `gui_v2_results/gui_v2_summary.md`，必要時再上傳幾份 `v2_*.json`。

## 二、量測內容

| 分類 | 代表階段 | 解讀 |
|---|---|---|
| UI | `ui.poll`, `ui.log_batch`, `ui.refresh_map`, `ui.refresh_lists` | 若個別函式耗時達 1～3 秒，較可能是主執行緒自己執行太久 |
| OCR | `ocr.preprocess`, `ocr.recognize`, `ocr.backend` | 比較影像前處理與 OCR 辨識時間 |
| 個資 | `pii.boxes`, `pii.detect`, `pii.presidio` | 比較 PII 候選框與 NER/規則處理耗時 |
| 人臉 | `face.detect`, `face.model_detect` | 視專案版本是否有可攔截的入口 |
| PDF | `pdf.zoom_ocr`, `pdf.page.scanned`, `pdf.secure_export` | 區分 2×/3× OCR、掃描頁處理及安全重建 |
| 其他 | `batch.dispatch`, `media.file`, `map.save` | 批次入口、單檔分派、還原對照表儲存 |

**重要**：`inclusive_s` 包含內部子階段，不能把所有 inclusive 數字直接相加。例如 `pdf.mask` 包含各頁的 OCR 與 PDF 輸出。`exclusive_s` 扣除偵測到的巢狀階段，較適合比較成本。

`slow_ui_gaps` 只在延遲 ≥500ms 時記錄；`phase_samples` 是監控執行緒看到的 Worker 階段、`overlapping_stages_s` 是同時段量測函式的時間交集。**時間重疊不等於因果，也不能直接判定 GIL 問題**。若觀察到 3 秒空窗而 sampler 也停止記錄，可能是 GIL 長時間競爭、系統排程或原生運算阻塞，需進一步分析。

## 三、如何判定下一步

- `ui.poll` 或 `ui.log_batch` 的 `max_call_s` 接近 3 秒：再定位 Tk 主執行緒呼叫、長日誌或刷新列表。
- `ui.poll` 都很短，但 `ocr.backend`／`pii.detect` 與卡頓有時間重疊：先做控制變因測試（僅 OCR / 僅 NER），不要直接認定因果。
- `pdf.zoom_ocr` 的 3.0 倍解析度特別耗時：評估是否能用「有品質門檻的 fallback」避免每頁都重跑；更動前後需重新驗證全部 27 個文字 PII 與人臉。
- `pdf.secure_export` 很耗時：檢查 DPI、輸出圖格式及記憶體峰值，**不能**退回會保留附件/原圖的原始 `doc.save()`。
- RSS 每次結束都回不到類似基準：另做同一程序 5 次連續測試，再分析快取或洩漏。

## 四、限制與安全

- 此工具以 monkeypatch 包裝現有函式，保留原有輸入、回傳值和例外；會有**少量測速開銷**，故不宜拿它的秒數當作「未安裝測速器」的精確效能。
- 不收集 OCR 文字、姓名、臉部裁切、檔案路徑、檔名或還原對照表；只保存輸入的副檔名數量與輸出數量。
- 不會強制啟用/關閉 CPU、GPU、網路、模型。沿用你原本的離線執行環境。
- 因無法在這裡使用你的 Windows、模型和 GUI，本機測試只涵蓋監測器行為；實際瓶頸必須看 Windows 回傳的 `v2_*.json`。
- V2 尚未包含 py-spy、ETW 或 native stack tracing，不提供「GIL 已證實」的判定。
- 若監測入口顯示 `instrumented_stages` 未包含某一模組，可能是本機版本函式名稱不同；這不代表該功能未執行。
