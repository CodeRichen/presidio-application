# V5 GUI 卡頓改善完成後：PDF 安全與遮蔽 Ground Truth A/B 驗收

## 目標與現有測試結果

- 之前 V5 三次 GUI 性能測試：最大 UI 排程延遲 416.98 / 190.58 / 233.32 ms，三次均無 >500ms 延遲；證明**效能方面初步通過**。
- 這套工具做下一個品質關卡：**將 V5 開啟與關閉，對完全相同的七頁合成 PDF 各跑一次現有 `handler_media.mask_pdf()`**，驗證是否仍能達到 27 個已知文字 PII、17 個記錄的人臉框遮蔽，以及純影像 PDF 結構。
- 不修改 `ui.py`、`handler_media.py`、`pdf_secure_output.py`、模型或測試資料。使用 `PII_PDF_EXPORT_V5=0/1` 的子程序環境變數控制同一份程式碼的兩種輸出方法。

## 內容

- `v5_acceptance.py`：執行兩次獨立的完整遮蔽流程、檢查 Ground Truth 與 PDF 結構、比對輸出 PDF 每一頁的渲染像素；輸出安全摘要
- `run_7page_pdf_test.py`、`audit_7page_pdf.py`：從先前七頁 PDF 整合測試包沿用的原版測試與評估程式
- `expected_text_and_faces.json`：先前七頁測試使用的相同 Ground Truth
- `test_gate.py`：不依賴 Windows OCR/NER 的工具單元測試
- **不包含原始 PDF**。請使用原本的 `PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf`（它是未遮蔽的測試樣本，僅本機保留）。工具會先比對預設合成資料的 SHA-256，以免誤處理其他文件。

## 1. 解壓縮

把 ZIP 解壓到 `presidio_app_allen` 專案根目錄，保留 `PII_V5_Security_Acceptance/` 資料夾。請先關閉 GUI，不要同時跑其他測試，以免共用模型資源。

在 PowerShell 從專案根目錄執行：

```powershell
.\.venv\Scripts\python.exe .\PII_V5_Security_Acceptance\test_gate.py
```

預期 7 tests OK。

## 2. 正式跑 V5 安全回歸

```powershell
.\.venv\Scripts\python.exe .\PII_V5_Security_Acceptance\v5_acceptance.py `
  --source .\PII_Face_PDF_7page_Test\pii_face_7page_scan_source.pdf `
  --results-dir .\v5_security_acceptance_results `
  --keep-v5-pdf
```

**注意**：這不是 GUI 心跳速度測試，而是兩次完整處理，可能各花約 200 秒。`--keep-v5-pdf` 僅在自動驗收全部通過後才保留一份已遮蔽合成 PDF，方便目視驗收。如果不需要保留，移除這個參數。

若你的合成原始 PDF 放在其他資料夾，修改 `--source` 路徑。檔案內容需為原先那份原始合成樣本（SHA-256 固定，拒絕其他文件）。

兩次測試的處理程序會在**自己的環境**設定：

- `PII_PDF_EXPORT_V5=0`（baseline）與 `PII_PDF_EXPORT_V5=1`（V5）
- `PII_OCR_BACKEND=rapid`
- `PII_NER_BACKEND=ac`
- `PII_FACE_ENABLED=1`
- **`PII_MEDIA_FP_PROFILE=synthetic_face_test`**：只在七頁合成測試用，真實資料不得開啟

完成後自動移除暫存 PDF、**含有原始人臉與文字裁圖的私有還原 map**；不將它們寫入報告。若 Python 被強制終止，仍應檢查 `%TEMP%` 是否留有 `pii_v5_gate_*` 暫存目錄，並自行安全清除。

## 3. 看結果

```text
v5_security_acceptance_results/
  v5_security_acceptance.json
  v5_security_acceptance.md
  v5_masked_for_manual_review.pdf    # 只有開啟 --keep-v5-pdf，且全部通過時產生
```

**PASS_AUTOMATED 的必要條件**：兩次各自輸出 7 頁 PDF；27/27 已知文字 PII 區域黑色覆蓋率 >=98%；17/17 記錄的人臉框黑色覆蓋率 >=98% 且每頁偵測框數不少於原先的目視估計；輸出附件、可提取文字、註解、連結、表單都為零；每頁僅一個平面影像；只有單一 EOF；V5 與 baseline 七頁**渲染後 RGB 像素完全一致**。

**重要限制**：人臉的 17 個數量是原始測試集的目視估計，不是全部臉的人工精確框真值。全部已記錄 FACE 框遮住，**不能證明沒有漏掉別的人臉**。仍需目視檢查每一頁（小臉、側臉、口罩、遮擋）與無必要的黑框，以及完整的 PDF 安全性測試。這套 A/B 測試不檢查真實文件的全面 PII 召回率。

## 4. 之後進入 V6

如果此套測試 `PASS_AUTOMATED` 且逐頁目視也通過，就先固定 V5 版程式作基準，再針對 V2 已定位的 `ocr.backend`（平均約 135 秒/批次，15 次呼叫）建立 V6 OCR 對照測試。**先保留目前的兩個 zoom 辨識**；每次只更動一個 OCR 參數或策略，並重新跑 Ground Truth、PDF 安全與 GUI 延遲測試。不能直接用省略高 zoom 取代完整評估。

## 5. 回傳給我

請上傳 `v5_security_acceptance_results/v5_security_acceptance.json` 和 `.md`。**不要上傳任何 PRIVATE_restore_map_DO_NOT_SHARE.json**，也不必提供原始 PDF。若有任何 FAIL，請提供報告中的 `error_type`，及你自行去除個資與本機絕對路徑的錯誤訊息。
