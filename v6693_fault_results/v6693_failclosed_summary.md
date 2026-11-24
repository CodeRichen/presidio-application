# V6.6.9.3：安全失敗處理故障注入（隔離模擬）

- 狀態：**FAULT_MOCK_TESTS_PASS_SOURCE_REVIEW_REQUIRED**
- 合成故障注入：15/15 通過
- 已檢查正式原始碼 AST，但未修改或 import 正式程式。
- 此測試使用假 PDF 位元組與模擬安全稽核，不是完整 PDF/GPU/GUI 實測。
- 不允許用本報告解鎖真實文件處理。

| 靜態風險 | 是否存在 |
|---|---|
| core_exception_may_continue | True |
| core_error_may_copy_original | True |
| scan_may_copy_unmasked_tree | True |

| 故障注入 | 防護結果 |
|---|---|
| BASELINE_APPROVED_MOCK | PASS |
| GPU_WORKER_CRASH | PASS |
| OCR_TIMEOUT | PASS |
| PDF_EXPORT_ERROR | PASS |
| PARTIAL_OUTPUT_EXCEPTION | PASS |
| MISSING_OUTPUT | PASS |
| EMPTY_OUTPUT | PASS |
| SECURITY_AUDIT_REJECTED | PASS |
| SECURITY_AUDIT_EXCEPTION | PASS |
| MALICIOUS_UNTRUSTED_PATH | PASS |
| MODIFIED_SOURCE_REJECTED | PASS |
| SECOND_ITEM_FAILURE_ROLLBACK | PASS |
| WRONG_RESULT_TYPE | PASS |
| OUTPUT_SYMLINK_REJECTED | PASS |
| SOURCE_BYTES_PRESERVED | PASS |
