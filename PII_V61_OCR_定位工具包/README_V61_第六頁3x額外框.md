# V6.1｜定位第 6 頁的 3× OCR 額外遮蔽框

## 已知情況與目標

前一份 V6 A/B 測試：雙解析度 **194.684 秒**、僅 2× **132.124 秒**；雖然 27/27 已知文字 PII 和 17/17 已記錄人臉框都通過，但 **第 6 頁**有 **1 個由 3× OCR 補到的框**。僅 2× 輸出在 90 DPI 比對下，少了 **388 個黑色遮蔽像素**。這可能是真正補抓的敏感資訊，也可能是誤判；目前不知道。

**目標：**只針對第 6 頁跑一次 2× 和 3× OCR，輸出這個新增框的「類別、來源類型、在頁面上的相對百分比位置、與已標註文字 PII 的幾何重疊比例」。**不保存** OCR 原字串、姓名、電話、Email、未遮蔽影像、模型 stdout、PDF 或還原 map。

**安全約束：**只允許固定 SHA-256 的七頁合成資料；本工具**不修改** `handler_media.py`、`ui.py`、`pdf_secure_output.py`、OCR 模型及 V5 子程序。正式去識別化仍使用 2×+3× OCR。測試專用 `PII_MEDIA_FP_PROFILE=synthetic_face_test` 只在子程序內啟用，不能在真實文件環境開啟。

## 安裝

1. 將本 ZIP 解壓縮到 `presidio_app_allen` 專案根目錄（應看到 `PII_V61_OCR_定位工具包/v61_locate.py`）。
2. 保留已安裝的 `PII_V5_Security_Acceptance/expected_text_and_faces.json` 和原合成 PDF `PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf`。
3. 關閉目前 GUI，從 PowerShell 切換至專案根目錄。

## 執行命令

```powershell
.\.venv\Scripts\python.exe .\PII_V61_OCR_定位工具包\test_v61.py

.\.venv\Scripts\python.exe .\PII_V61_OCR_定位工具包\v61_locate.py --check-only

.\.venv\Scripts\python.exe .\PII_V61_OCR_定位工具包\v61_locate.py --results-dir .\v61_ocr_results
```

這會在隔離子程序中載入你與 V6 相同的 RapidOCR/PII 模型，只處理第 6 頁，因此不需再次完整跑七頁。模型載入可能仍需一些時間；不會寫入來源文件。

## 產出

- `v61_ocr_results/v61_page6_extra.json`
- `v61_ocr_results/v61_page6_extra.md`

產出中，`bbox_percent_xyxy` 代表 `[左, 上, 右, 下]` 的相對頁面百分比（0–100）。可以據此在你的**本機**原始合成 PDF 第 6 頁定位（不要上傳未遮蔽的真實文件或私有 map）。`overlap_known_text_label` 表示是否與既有合成文字 Ground Truth 相交；未相交**不能**判定非 PII。來源類型有白名單，未知來源顯示 `OTHER`。

若 `count_consistent_with_v6` 為 `false`，表示這次局部測試未完全重現先前全批次的 1 個新增框，需確認模型版本、快取或其他環境因素，**不能**逕行部署選擇性 3×。

## 選擇性 3× OCR 的下一個決策

- 若新增框是**敏感資訊**：保留這類場景的 3× OCR；需要更廣的獨立 Ground Truth 才能定義可靠觸發條件。
- 若新增框是**非敏感資訊**：也不能因此把「其餘 6 頁沒新增框」變成安全跳過規則。優先研究不降低辨識覆蓋的加速（如 OCR 後端批次處理、前處理優化）。
- 任何預備部署的自動跳過策略，都須在大量不同字型、小字、低對比、模糊影像與多筆 PII 的合成文件中與雙解析度基準對照，核對完整 PII Ground Truth，而非只看 27/27 的有限測試。

本工具是**診斷**，不是完整安全認證。報告狀態固定 `MANUAL_REVIEW_REQUIRED`。
