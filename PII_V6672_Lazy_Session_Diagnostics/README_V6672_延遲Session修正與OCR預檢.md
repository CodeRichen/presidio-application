# V6.6.7.2：RapidOCR Session 初始化時機修正／安全預檢

## 這版為何必要？

V6.6.7 完整 PDF 試驗：CPU 測試成功（182.285 秒），hybrid 子程序在約 31 秒後 `CHILD_FAILED`，無法取得完整 PDF GPU 時間。隨後 V6.6.7.1 報告：`INCOMPLETE_MODEL_SESSIONS`，階段 `RAPIDOCR_CONSTRUCT`，且 adapter 預檢尚未開始。

經比對測試程式，發現兩項可能讓診斷**過早誤判**的差異：

1. V6.6.7.1 的 GPU probe **先匯入 RapidOCR，才安裝 InferenceSession 攔截**。如果 RapidOCR 匯入時綁定了原始建構函式，診斷器可能看不到 Session，即使 Session 確實存在。
2. V6.6.7／V6.6.7.1 在 `RapidOCR()` **建構後、第一次 OCR 前**就要求 det／cls／rec 三種 Session 全部出現。如果模型採延遲初始化，會被提前拒絕；V6.6.6 則是在已執行 OCR 後才驗證。

這些是從工具碼找到的可疑原因，**不是已在 Windows GPU 上確認的根因**。

## V6.6.7.2 修正內容

- `v6672_gpu_probe.py`：**先攔截 Session，再匯入 RapidOCR**；記錄「建構後」和「合成 OCR 後」分別出現哪些模型 Session。至少要確認 det 有**實際 CUDA Node**，才會進入第二階段。
- `v667_gpu_worker.py`（新版實驗副本）：不在 `RapidOCR()` 建構後立刻拒絕缺少 Session，而是先接受 approved synthetic OCR 請求；**全部 OCR 完成後才提取模型路由證據**。
- `v6672_adapter_probe.py`：使用固定 SHA-256 七頁合成 PDF **第 1 頁** 2× 與 3× 圖片，比較正式 `handler_media.ocr_words()` 和新版實驗 worker 的完整回傳字典。文字、行編號、字框只在 RAM 比較；有差異立即拒絕，不修改正式程式。
- 最終通過要求：2/2 組原始 OCR adapter **精確相等**，且 det CUDA／cls CPU／rec CPU 各有真實 Node events，三種 Session 與 3 份 profile 全部檢查成功。

> 重要：第一階段只驗證 det CUDA 的小型合成運算；不要求合成圖片一定觸發 cls／rec。三模型完整驗證留到第 1 頁的 2×／3× 預檢後再進行。**不會將缺少完整路由的結果視為通過**。

## 安全範圍

- **不改動** `handler_media.py`、`ocr_backend.py`、`.venv`、`.venv_ocr_gpu` 或之前的 V6.6.7 工具。
- 使用**獨立測試副本**的 GPU worker，僅處理內建合成字串與固定 SHA-256 合成 PDF 第 1 頁。
- 不執行七頁 PDF 遮蔽／PII／人臉／PDF 安全輸出回歸；**不得用在真實文件**。
- `PII_MEDIA_FP_PROFILE=synthetic_face_test` 僅設定在獨立診斷子程序，絕不可用在真實文件／正式 GUI。
- OCR 圖像以匿名本機管道進出，不寫入報告；不保存 OCR 原文、字框座標、文字雜湊、PDF、私有 map、原始 ORT profile 或 traceback。
- 臨時 profiling 資料在正常結束時清理；強制中止後可能須手動檢查 `%TEMP%` 中 `pii_v6672_` 和 `pii_v667_worker_` 前綴目錄。

## Windows PowerShell 操作

將 ZIP 解壓到 `presidio_app_allen` 專案根目錄，保留原本兩套虛擬環境、`handler_media.py`、`ocr_backend.py` 和 `PII_Face_PDF_7page_Test/`；原來的 V6.6.7、V6.6.7.1 可保留但**不用執行**。

### 1. 自我測試（不載入 OCR 模型）

```powershell
.\.venv\Scripts\python.exe .\PII_V6672_Lazy_Session_Diagnostics\test_v6672.py
```

### 2. 環境檢查（只核對路徑與 PDF SHA-256）

```powershell
.\.venv\Scripts\python.exe .\PII_V6672_Lazy_Session_Diagnostics\v6672_diagnose.py --check-only
```

### 3. GPU 初始化與 OCR Adapter 預檢（不重跑完整 PDF）

```powershell
.\.venv\Scripts\python.exe .\PII_V6672_Lazy_Session_Diagnostics\v6672_diagnose.py `
  --results-dir .\v6672_lazy_session_results
```

**即使測試發現失敗，CLI 可能回傳非零狀態碼。** 這不是程式安裝失敗，請查看 JSON／MD 報告中的固定 `status` 及 `stage`。

## 報告

```text
v6672_lazy_session_results/
├── v6672_lazy_session_summary.json
└── v6672_lazy_session_summary.md
```

| 狀態 | 意義／後續處理 |
|---|---|
| `GPU_DET_NOT_VERIFIED` | 合成 GPU det 未取得 CUDA kernel 執行證據；查看 `gpu_probe.stage`、`error_type`、兩個 Session 計數 |
| `WORKER_OR_ADAPTER_EXCEPTION` | worker 通訊／正式 OCR 介面／PDF 預檢中有例外；查看 `adapter_probe.stage` 與 `error_type` |
| `OCR_ADAPTER_MISMATCH` | 文字、字框、行編號或格式不相容；不能執行完整 PDF；請提供最新版 `ocr_backend.py` 與 `handler_media.py` 供修正橋接器 |
| `PROVIDER_ROUTE_NOT_VERIFIED` | 模型未全部依 det CUDA、cls/rec CPU 實際運算；不得採用 |
| `WORKER_AND_ADAPTER_PREFLIGHT_OK` | 初始化／路由／第 1 頁 2×、3× adapter 預檢通過，才有資格進下一輪完整 PDF 安全 A/B |

完整 PDF 後續仍需安全驗收：已標註 27/27 文字 PII、已記錄 17/17 人臉框、PDF 結構安全、7/7 頁像素一致與真正的總處理時間；本版無法提供這些結論。

## 本機開發驗證

- `test_v6672.py`：28/28 測試通過，包含假的 RapidOCR **匯入時綁定 Session**與**首次 OCR 才延遲建立 Session**的測試。
- 模擬專案中的 `--check-only` 通過。
- **沒有在你的 Windows RTX 4060 上實際執行** GPU／RapidOCR／正式 OCR Adapter 預檢；通過本機測試不代表 Windows 環境已修復。
