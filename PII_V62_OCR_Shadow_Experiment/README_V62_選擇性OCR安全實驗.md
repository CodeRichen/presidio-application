# V6.2：選擇性 3× OCR 的安全性「影子模式」

## 為什麼不是直接套用選擇性 OCR？

V6 在原本七頁合成 PDF 上，雙解析度約 194.68 秒、僅 2× 約 132.12 秒，後者雖然覆蓋了已標註的 27 個 PII，卻少了第 6 頁一個 3× 額外偵測框（於 V6.1 研判可能是衣服圖樣誤判）。**已知框通過不等於沒有未知 PII 漏判**。

本工具用較保守的方法：**每一頁的 2× 和 3× 仍會真的執行**，但在 2× 結束後，根據當下的文字尺寸、辨識字數、PII 框數、影像對比與邊緣強度，先紀錄「如果真的跳過 3×」的決策。接著使用 3× 的完整結果和獨立 Ground Truth，計算這個決策的風險及**理論上可節省的 3× 執行時間**。

**注意：這是研究用啟發式門檻，不是可靠的生產安全判斷。** 未被 2× 看見的小字，無法從 2× 的框高直接證明不存在；完整 OCR 流程和遮蔽必須繼續使用雙解析度。

## 安全邊界

- 不修改 `handler_media.py`、`ui.py`、`pdf_secure_output.py`、模型或 GUI 設定。
- 不對真實文件執行跳過 3× 的策略，也不提供讓正式程式使用的環境開關。
- 內建生成 **8 頁純影像 PDF、30 個已知合成 PII 標註**（字小、極小字、低對比、模糊、大小字混合、多筆資料、純文字等）。姓名、電話和 Email 都是虛構示例。
- 合成 PDF 和標註只在系統 TemporaryDirectory；模型執行時不寫入原始 OCR 內容或私有還原 map。子程序 stdout/stderr 不記錄，避免模型印出文字值。
- 另外可選擇已先前驗證 SHA-256 的原版七頁合成 PDF（27 個已知文字 PII）。不允許任意 PDF。
- 只有輸出 JSON、Markdown 報告的計數、覆蓋率、時間和判定原因可以分享。不要分享未遮蔽合成 PDF，尤其不要用真實個資文件取代它。

## 安裝與執行（Windows PowerShell）

將整個 `PII_V62_OCR_Shadow_Experiment` 資料夾解壓到 `presidio_app_allen` 專案根目錄，關閉正在執行的 GUI。

```powershell
# 確認在 presidio_app_allen 資料夾
.\.venv\Scripts\python.exe .\PII_V62_OCR_Shadow_Experiment\test_v62.py

# 僅生成與驗證全新合成測試 PDF，不載入模型
.\.venv\Scripts\python.exe .\PII_V62_OCR_Shadow_Experiment\v62_experiment.py --check-only

# 執行 8 頁硬案例 2×/3× shadow 診斷
.\.venv\Scripts\python.exe .\PII_V62_OCR_Shadow_Experiment\v62_experiment.py --results-dir .\v62_ocr_results
```

如需加上原有七頁資料進行跨案例檢查（需先保留 `PII_V5_Security_Acceptance/expected_text_and_faces.json` 和 `PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf`）：

```powershell
.\.venv\Scripts\python.exe .\PII_V62_OCR_Shadow_Experiment\v62_experiment.py `
  --include-seven-page `
  --results-dir .\v62_ocr_results
```

所需依賴和版本請沿用已通過 V5/V6 的環境：PyMuPDF、Pillow、RapidOCR、Presidio 與中文 NER。若缺少中文字型，請確認 Windows 字型 `msjh.ttc` 或 `msyh.ttc` 存在。

## 報告

- `v62_ocr_results/v62_ocr_shadow_summary.json`
- `v62_ocr_results/v62_ocr_shadow_summary.md`

每頁會有 `zoom2`、`dual`、`selective_shadow` 的 **獨立合成 PII bbox 覆蓋率**，`3x_extra_boxes`、`would_skip_3x`、`reasons` 與 2×/3× 耗時。不輸出文字值和圖片。

### 驗收解釋

- `BASELINE_GROUND_TRUTH_GAPS`：完整 2×+3× 都沒遮到所有已知 PII，**先修復辨識或標註問題**，不能評價候選安全。
- `CANDIDATE_LOSES_KNOWN_PII`：影子策略相較雙解析度多漏掉已知 PII，**拒絕採用**。
- `SHADOW_SYNTHETIC_ONLY`：在目前合成案例、目前門檻下沒有新增已知漏遮；**不代表可以部署真實文件**。
- `INCOMPLETE` / `TIMEOUT` / `ERROR`：先解決執行問題，不能做安全結論。

即使候選和 baseline 同樣通過，仍需更多不同文件格式的獨立人工 Ground Truth 和真實影像條件測試，才能探索受控部署。辨識所處理的影像 PDF 這次**沒有**正式呼叫遮蔽與 V5 PDF 輸出：本實驗只分析 OCR 輸出框，而非 PDF 安全驗收。

## 建議後續

將兩份摘要上傳。若候選遇到已知漏遮，保留正式雙解析度策略，優先改進 OCR backend 的重用、批次處理、GPU/CPU 配置或 PII 偵測快取。若沒有新增漏遮，下一輪仍需加入獨立的難例和逐頁人工覆核；不能只靠一個啟發式規則決定真實文件是否安全。
