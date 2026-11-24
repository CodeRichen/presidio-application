# V6.5.3｜PyTorch 中文 NER 執行緒：第一次／第二次處理穩定性驗證

## 為什麼做？

上一輪 V6.5.2 使用 PyTorch 預設 14 個 CPU intra-op 執行緒與候選 4 個執行緒比較。完整處理時間：

- 預設：270.602、211.136 秒
- 4 執行緒：209.384、209.462 秒
- 中位數改善 13.06%，但 `code_detect` 僅改善 1.53%；預設第一次較第二次慢了約 59.5 秒。

**重要更正：V6.5.2 已使用 AB、BA 交錯順序，並非完全沒有控制執行順序。** 這次改測前一輪沒有拆開的「同一 Python 子程序內連續兩次完整 PDF 遮蔽」，同時量測子程序準備階段、首次處理及第二次處理。目的在於檢查速度差是否主要受到首次初始化、暖機或快取影響。

**此工具只是測試，不是正式加速修補。** 保留所有 2×+3× OCR、`handler_code.detect`、中文 NER、臉部偵測及 V5 PDF 隔離安全輸出。**不修改正式程式檔與模型，不改寫推論設定。**

## 目錄與需求

將 `PII_V653_Torch_Stability` 資料夾解壓至專案根目錄 `presidio_app_allen`，與以下檔案同層：

```text
presidio_app_allen/
├─ .venv/Scripts/python.exe
├─ handler_media.py
├─ handler_code.py
├─ pdf_secure_output.py           # 已整合 V5
├─ pdf_secure_v5.py
├─ pdf_secure_v5_worker.py
├─ PII_Face_PDF_7page_Test/
│  └─ pii_face_7page_scan_source.pdf
├─ PII_V5_Security_Acceptance/
│  ├─ v5_acceptance.py
│  ├─ run_7page_pdf_test.py
│  ├─ audit_7page_pdf.py
│  └─ expected_text_and_faces.json
└─ PII_V653_Torch_Stability/
   ├─ v653_child.py
   ├─ v653_benchmark.py
   ├─ test_v653.py
   └─ README_V653_第一次第二次處理穩定性驗證.md
```

限用 SHA-256 驗證通過的固定七頁**合成** PDF；不可拿這個工具處理真實文件。使用專案原有虛擬環境；不用安裝新套件，也不需要開啟網路。

## 1. 自動化測試／環境檢查

在 PowerShell 中切換到 `presidio_app_allen` 專案根目錄，關閉正在執行的 GUI。

```powershell
.\.venv\Scripts\python.exe .\PII_V653_Torch_Stability\test_v653.py

.\.venv\Scripts\python.exe .\PII_V653_Torch_Stability\v653_benchmark.py --check-only
```

`--check-only` 只驗證環境與固定合成 PDF 的 SHA-256，不會載入 OCR 模型或執行正式遮蔽。

## 2. 先做一組煙霧測試（驗證整條流程）

```powershell
.\.venv\Scripts\python.exe .\PII_V653_Torch_Stability\v653_benchmark.py `
  --threads 4 `
  --repeats 1 `
  --results-dir .\v653_torch_smoke
```

這會進行：原始設定子程序內「第一次+第二次」各一份七頁 PDF，以及候選 4 執行緒子程序內「第一次+第二次」各一份，**合計四次完整處理**。只有一組時輸出狀態應為 `INSUFFICIENT_REPEATS`，不是有或沒有加速的結論。

## 3. 正式穩定性比較：5 個交錯配對

```powershell
.\.venv\Scripts\python.exe .\PII_V653_Torch_Stability\v653_benchmark.py `
  --threads 4 `
  --repeats 5 `
  --results-dir .\v653_torch_5pairs
```

五組子程序順序為 **AB → BA → AB → BA → AB**（A=PyTorch 原始執行緒數，B=4 執行緒）。每個 A/B 子程序都**重新啟動 Python**，並各自執行兩次完整七頁合成 PDF 遮蔽；五組共 **20 次完整處理**。測試較耗費運算時間，請讓程式自然執行結束；不要強制中斷。

如果想先調整 2 執行緒，請完成 4 執行緒這一組後再開新資料夾測試，避免同時執行 CPU 密集工作造成量測干擾。

## 報告內容

完成後輸出：

```text
v653_torch_5pairs/
├─ v653_torch_stability_summary.json
└─ v653_torch_stability_summary.md
```

報告包含：

- `pre_run_setup_s`：驗證固定檔、載入 Torch、`handler_code`、測試 runner 等**子程序內準備時間**，不等於純模型初始化時間。
- `first.processing_s`：同一 Python 子程序**第一次**完整 PDF 遮蔽耗時（不含 GT 驗收時間）。
- `second.processing_s`：同一子程序**第二次**完整 PDF 遮蔽耗時（同一合成來源、獨立輸出和 private map）。
- `code_detect_wall_s`：每次遮蔽期間的 `handler_code.detect` 累積時間（不可與完整時間加總）。
- 原始組與候選組第一次/第二次中位數、四分位數、配對改善率，以及候選組究竟有多少組變快。

### 安全驗收（每一次完整處理）

1. 已標註文字 PII 遮蔽 27/27，且已記錄的 17 個 FACE-map 框覆蓋達標。
2. PDF 安全結構 PASS（七頁 raster-only、無文字層、附件、連結、表單、註解）。
3. 每一配對做**四個**像素一致性檢查：原始/候選的第一次、原始/候選的第二次、原始自己前後兩次、候選自己前後兩次；每個檢查均需七頁 150 DPI RGB 像素一致。
4. 實際生效的 PyTorch 執行緒數等於測試設定；`code_detect` 呼叫次數兩組及兩次一致。

**固定資料的 27/27 與 17/17 不是對所有實際文件的辨識保證；17/17 也不是全部人臉的獨立偵測率。** 本工具也不測 GUI 回應性。

### 報告狀態

| 狀態 | 判定 |
|---|---|
| `PROMISING_WARM_AB_ONLY` | 至少五組；第二次處理中位數改善 ≥10%，至少 80% 配對變快，且 `code_detect` 中位數也有改善；**仍不可直接部署** |
| `NO_REPRODUCIBLE_GAIN` | 第二次處理未達上述穩定改善條件；**保持原始設定** |
| `INSUFFICIENT_REPEATS` | 少於五組；能完成驗收但不足以評估穩定性 |
| `NO_EFFECTIVE_CHANGE` | 候選組與原版實際 PyTorch 執行緒數相同 |
| `TUNING_NOT_APPLIED` | 候選執行緒設定沒有真正生效 |
| `INSTRUMENTATION_MISMATCH` | PII 偵測呼叫次數不一致 |
| `PIXEL_DIFFERENCE` | 任一 PDF 逐頁像素檢查不一致，拒絕加速結論 |
| `SECURITY_GATE_FAILED` | 固定 GT、PDF 結構或子程序驗收失敗 |
| `INCOMPLETE` | 資料不完整，無法比較 |

## 隱私與局限性

- stdout/stderr 不寫入報告，避免意外保存含 PII 的日誌。
- 所有 PDF、遮蔽輸出與 `PRIVATE_restore_map_DO_NOT_SHARE.json` 均只放在暫存資料夾；**正常完成會刪除**。若系統中止、強制結束或發生非正常狀況，請檢查 Windows 暫存目錄中 `pii_v653_` 開頭的資料夾，刪除殘留資料。
- 不要將私有 map、原始照片或任何 OCR 原始文字上傳，只需回傳 `.json` 與 `.md` 報告。
- 第二次處理的結果受到頁面與磁碟快取、模型暖機及 Windows 背景負載影響，**不等於模型的純推論加速**，更不能代表陌生真實文件的冷啟動速度。
- 若最終結果仍不穩定，建議停止 PyTorch 執行緒調參，將下一輪優化重心轉回 OCR 流程，而非犧牲辨識次數或遮蔽完整性。
