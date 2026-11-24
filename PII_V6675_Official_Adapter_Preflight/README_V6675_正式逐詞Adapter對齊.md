# V6.6.7.5：正式 RapidOCR 逐詞 Adapter 對齊與安全預檢

## 根因已從你提供的正式程式查明

- 正式版 `ocr_backend.rapid_ocr_words(image)` 使用 `engine(image, return_word_box=True)`，並用 `ocr_backend.rapid_result_to_words(result, image.width, image.height)` 轉為逐詞資料。
- 舊 V6.6.7.x 混合 GPU worker 僅使用 `engine(image)`，再用自製 `result_to_words` 把每行當成一筆，也沒有保留可選的 `conf`。
- V6.6.7.4 的 2× 正式版 114 筆、混合版 11 筆；3× 正式版 115 筆、混合版 11 筆，與以上程式差異一致。這是高度可信的根因，但修正能否在 Windows GPU 環境通過仍待實測。

## 修正策略

1. **GPU det，CPU cls/rec**：沿用 V6.6.6 驗證過的 ONNX Runtime Session 實驗路由；只作用在隔離的 `.venv_ocr_gpu` 子程序。
2. **依正式版取得 word box**：GPU worker 使用 `return_word_box=True`。
3. **不複製與重寫正式轉換規則**：worker 從專案根目錄匯入 `ocr_backend.rapid_result_to_words`，保留正式版既有的 word_results、可信度、polygon 對齊／邊界裁切及失敗時整行遮蔽回退。
4. **同 CPU thread 設定**：預設 `PII_RAPID_CPU_THREADS=4`，沿用正式 `_rapid_engine` 的值，合法值 0,2,4,6,8。不要為了這次實驗修改正式環境。
5. **只接受符合要求的輸出**：文字、每筆 bbox（left/top/width/height）、line_id 完全一致，並檢查 `conf` 欄位存在性一致且數值差異不超過 0.002。任何額外欄位、筆數、拆詞、字框不同，都不通過。原始文字、圖片、座標、還原 map、profile 不會寫入報告。
6. **只測合成檔第 1 頁**：2×、3× 各一次。這不是完整 PDF 安全／速度 A/B。

## 安裝及執行 (Windows PowerShell)

將 ZIP **完整解壓到 `presidio_app_allen` 專案根目錄**，保留 `PII_V6675_Official_Adapter_Preflight` 資料夾名稱；專案應已存在正式 `.venv`、`.venv_ocr_gpu`、`handler_media.py`、`ocr_backend.py` 以及固定合成 PDF。

```powershell
# 請先 cd 到 presidio_app_allen 的專案根目錄

# 1. 本機單元測試；只用合成字串，不啟動真實 OCR 引擎
.\.venv\Scripts\python.exe .\PII_V6675_Official_Adapter_Preflight\test_v6675.py

# 2. 唯讀檢查兩套 Python、正式 OCR 檔案與 SHA256 固定合成 PDF
.\.venv\Scripts\python.exe .\PII_V6675_Official_Adapter_Preflight\v6675_diagnose.py --check-only

# 3. 第一頁 2×/3× 的正式 CPU 與混合 GPU OCR 介面預檢
.\.venv\Scripts\python.exe .\PII_V6675_Official_Adapter_Preflight\v6675_diagnose.py `
  --results-dir .\v6675_adapter_results
```

**上傳這兩份安全統計報告：**

- `v6675_adapter_results/v6675_adapter_diagnosis_summary.json`
- `v6675_adapter_results/v6675_adapter_diagnosis_summary.md`

成功時，預期 `ADAPTER_PREFLIGHT_OK`、`pairs_checked=2`、`pairs_equal=2`、`provider_route_verified=true`，兩邊回傳筆數應一致（上一輪 2×=114、3×=115，實際結果以本輪為準）。若不通過，請以本輪 `mismatch_kind` 判斷是否還有分詞、座標、信心分數或硬體路由差異，**不要停用驗證、不要放寬文字或字框要求**。

## 安全界線

- 正式 `.venv`、`.venv_ocr_gpu`、`ocr_backend.py`、`handler_media.py`、其他正式程式及模型均不會被此工具改動。
- 只允許 SHA256 `df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49` 的固定合成 PDF。
- 不讀取真實個資文件；不得用於正式應用程式或使用者私人文件。
- CPU/GPU 的 OCR 比對僅在子程序記憶體裡執行，報告只寫布林值、筆數、行群組數、欄位統計、錯誤階段、Provider Node events；不寫原始文字／雜湊／框座標。
- GPU Session 對 det/cls/rec 的攔截是**實驗手法**，並非 RapidOCR 公開支援的獨立執行 provider 設定。
- `ADAPTER_PREFLIGHT_OK` **不代表** 27/27 文字 PII、17/17 已記錄人臉框、安全 raster PDF、7/7 頁像素及端到端加速已通過。這些必須在下一輪完整七頁 PDF 安全 A/B 再驗證。
- 未涵蓋 GUI、冷啟動、非合成文件或多語言資料品質。CPU 偵測 PII 時的現有隱私列印行為亦未在此版修改，因此請在已核准的合成資料上執行。
- 如果強制結束，在 Windows `%TEMP%` 下檢查 `pii_v6675_`、`pii_v6675_worker_` 為前綴的實驗暫存資料夾。正常退出會清理。

## 檔案說明

- `v667_gpu_worker.py`：隔離 GPU worker；使用正式 `return_word_box=True` 與 `rapid_result_to_words()`。
- `v667_bridge.py`：保留記憶體內的 PNG/JSON pipe 通訊；不將 OCR 文字寫入檔案。
- `v6675_adapter_probe.py`：在既有正式 `.venv` 內，以固定合成 PDF 第一頁做 2×／3× 對照並記錄 sanitized 報告。
- `v6675_diagnose.py`：檢查批准的 fixture／環境、啟動診斷及過濾結果，只寫合規的統計資訊。
- `test_v6675.py`：無 OCR 模型的相容性、防洩漏與流程模擬單元測試。

本機（非 Windows GPU）測試：35 項通過。尚未宣稱你的 RTX 4060 已完成本輪相容性實測。
