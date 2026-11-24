# V6.5.2：中文 NER（PyTorch）CPU 執行緒安全 A/B

## 為什麼做這項實驗？

V6.5.1 在 70 次 `handler_code.detect()` 抽樣中，`zh_ner()` 累積約 9.806 秒，Hugging Face Token Classification 約 7.991 秒，而 `torch._C._nn.linear` 的**自身耗時**約 5.056 秒。這證明優先調查 PyTorch 模型運算比盲目新增 Regex 快取更合理。上述累積時間互相重疊，不能相加，且 cProfile 有測試開銷。

本工具**不修改**任何正式程式、模型或辨識規則，不移除 3× OCR、不改動 V5 PDF 輸出。它只在測試子程序中透過 `torch.set_num_threads(N)` 改變 PyTorch **intra-op CPU threads**，並做同一份七頁合成 PDF 的原版/候選版完整 A/B 比較。

**這是效能實驗，不是已完成加速。** 若候選測試速度較快，但輸出 PDF 像素不一致、固定 GT 覆蓋不通過或 PDF 結構有問題，直接拒絕採用。

## 需要的專案結構

把整個 `PII_V652_Torch_AB` 解壓到 `presidio_app_allen` 根目錄：

```text
presidio_app_allen/
  .venv/Scripts/python.exe
  handler_media.py
  handler_code.py
  pdf_secure_output.py          # 已安裝 V5
  pdf_secure_v5.py
  pdf_secure_v5_worker.py
  PII_Face_PDF_7page_Test/
    pii_face_7page_scan_source.pdf
  PII_V5_Security_Acceptance/
    v5_acceptance.py
    run_7page_pdf_test.py
    audit_7page_pdf.py
    expected_text_and_faces.json
  PII_V652_Torch_AB/
    v652_benchmark.py
    v652_child.py
    test_v652.py
```

工具會用 SHA-256 限制只能處理**固定的合成七頁 PDF**。合成測試使用 `PII_MEDIA_FP_PROFILE=synthetic_face_test`，**不要**把此設定套到真實文件或正式 GUI。

## 執行（Windows PowerShell）

先關閉 GUI，確認在 `presidio_app_allen` 根目錄：

```powershell
.\.venv\Scripts\python.exe .\PII_V652_Torch_AB\test_v652.py

.\.venv\Scripts\python.exe .\PII_V652_Torch_AB\v652_benchmark.py --check-only
```

先做一組（確認能成功跑完整流程）：

```powershell
.\.venv\Scripts\python.exe .\PII_V652_Torch_AB\v652_benchmark.py `
  --threads 4 `
  --repeats 1 `
  --results-dir .\v652_torch_smoke
```

正式 A/B 建議至少兩組，先從 4 執行緒開始：

```powershell
.\.venv\Scripts\python.exe .\PII_V652_Torch_AB\v652_benchmark.py `
  --threads 4 `
  --repeats 2 `
  --results-dir .\v652_torch_4_results
```

如有需要再單獨測 2 執行緒，**使用不同輸出資料夾**：

```powershell
.\.venv\Scripts\python.exe .\PII_V652_Torch_AB\v652_benchmark.py `
  --threads 2 `
  --repeats 2 `
  --results-dir .\v652_torch_2_results
```

每一組原版與候選版使用**獨立 Python 子程序**（交錯執行 AB、BA，降低測試順序偏差）。`--threads` 限制在 `1,2,4,6,8,12,16`；0 僅表示原始設定，由工具自動執行。工具不會擅自修改 `torch.set_num_interop_threads` 或其他模型設定。

## 驗證與判定

每次進行：

- 27/27 個已標註文字 PII：各覆蓋率 ≥98%。
- 17/17 個先前記錄的人臉遮蔽框：各覆蓋率 ≥98%。**這不是全部人臉的獨立 Ground Truth。**
- 七頁 raster-only PDF 的結構檢查（無文字層、附件、連結、註解、表單，每頁一個影像）。
- 原版與候選版 PDF 以 150 DPI 渲染，七頁 RGB 像素 **7/7 全部一致**。
- 確認候選子程序 `torch_effective` 確實等於指定執行緒數，並且相對於原版不是完全相同設定。
- 另外記錄 `handler_code.detect` 的呼叫總次數與合計耗時；不保存輸入字串。

狀態：

| 狀態 | 解釋 |
|---|---|
| `PROMISING_AB_ONLY` | 完整時間中位數至少改善 10%，且 `code_detect` 耗時也改善；仍需擴充測試，不能直接上線 |
| `NO_CONFIRMED_GAIN` | 未達門檻或沒有明確趨勢；保留原本設定 |
| `NO_EFFECTIVE_CHANGE` | 原版執行緒數與候選相同，無有效 A/B |
| `TUNING_NOT_APPLIED` | 執行緒數設定未實際生效 |
| `PIXEL_DIFFERENCE` | 前後遮蔽 PDF 的可見像素不同，拒絕採用 |
| `SECURITY_GATE_FAILED` | 已知 GT 覆蓋或 PDF 安全驗收未通過 |
| `INCOMPLETE` | 測試未完成／子程序失敗／逾時 |

輸出：

```text
v652_torch_4_results/
  v652_torch_ab_summary.json
  v652_torch_ab_summary.md
```

報告不含 OCR 原文、姓名、電話、Email、雜湊或未遮蔽影像。中間的 PDF 和私有 map 保留在作業系統暫存目錄，程式正常結束會自動清理；強制終止可能留下檔案，務必檢查暫存資料夾。請勿將任何 `PRIVATE_restore_map...` 上傳。

## 限制

1. 每個子程序都包含模型初始化；兩組完整時間可能受磁碟快取、CPU 降頻、Windows 背景工作影響。兩次 A/B 不足以證明統計顯著性。
2. `torch.set_num_threads()` 影響該測試子程序的 PyTorch CPU 運算，並非只侷限於某一個 NER 模型。必須觀察整體時間與 `code_detect` 時間是否同步改善。
3. 這只測固定合成文件上的輸出一致性，不能證明其他真實文件上的遮蔽安全，也沒有測 GUI 心跳延遲。
4. 即使找到改善，也**不要**直接貼進正式 `ui.py` 或 `handler_media.py`。之後仍需真實 GUI A/B 和更廣泛的安全回歸。

建議回傳兩份報告 `v652_torch_ab_summary.json` 和 `.md`，不要上傳其他中間檔。
