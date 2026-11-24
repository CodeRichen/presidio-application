# YuNet 人臉 Ground Truth 評估工具

此腳本只使用**原始未遮蔽的 PDF**、`face_gt_annotator.py` 產生的 JSON，以及專案現有的 `face_detector.py`（YuNet）。不會改動 `core.py`、`handler_media.py`、Demo UI、原始 PDF、原始標註檔或還原對照表；執行不下載任何模型。

## 使用方法

將 `evaluate_yunet_face_gt.py` 放在專案 `presidio_app_allen` 根目錄（與 `face_detector.py` 同層）。

```powershell
.\.venv\Scripts\python.exe .\evaluate_yunet_face_gt.py `
  --pdf ".\PII_Face_PDF_7page_Test\pii_face_7page_scan_source.pdf" `
  --gt ".\PII_Face_PDF_7page_Test\face_ground_truth.json" `
  --output ".\PII_Face_PDF_7page_Test\face_yunet_evaluation.json"
```

* PDF 路徑必須是**你建立 Ground Truth 時的同一份原始 PDF**，程式會以 SHA256 核對。
* 若模型被停用（環境變數 `PII_FACE_ENABLED=0`）會明確拒絕評估；請保持與正式遮蔽相同的啟用設定。
* 結果檔已存在時不會覆蓋。請換新檔名或先備份舊結果。
* 如有 `face_detector` 模組不存在，請確認檔案與腳本同層。
* 如有模型不存在，請確認本機原有的 `models/face_detection_yunet_2023mar.onnx` 或 `PII_FACE_MODEL` 設定。

## 指標說明

評估採 2.0 倍 PDF 渲染，與 `handler_media.py` 掃描 PDF 的 YuNet 呼叫設定一致。依專案現有 `face_detector.detect_faces()` 預設參數偵測，使用不額外擴充的**原始人臉框**與人工框計算 IoU。

在 IoU >= 0.5 條件下，一個預測框最多只能對應一個人工標註框；採一對一最大匹配。

* TP：成功對到人工框的人臉
* FP：沒對到任何人工框的預測框
* FN：未被成功對到的人工標註人臉
* Precision = TP / (TP+FP)
* Recall = TP / (TP+FN)
* F1 = 2 × Precision × Recall / (Precision+Recall)

如果 `FN` 大於 0，請先檢查第幾頁及哪個 `missed_gt_indices`；如果 `FP` 大於 0，檢查該頁 `false_positive_prediction_indices`。JSON 的座標單位是 PDF points，沒有臉部原始圖片。

此結果衡量 **YuNet 偵測準確度**，不等於 PDF 最終黑框遮蔽覆蓋率，也不等於系統在任何資料集上的通用準確度。請對比原始 PDF、標註和實際的遮蔽 PDF 做安全檢查。
