# V6.6.9 正式 GUI 整合前：唯讀原始碼相容性檢查

- 靜態狀態：**SOURCE_SIGNATURE_CHANGED_REVIEW_REQUIRED**
- 沒有執行 GUI、OCR、PDF、模型或正式應用程式。
- 不修改任何正式程式、模型、虛擬環境、剪貼簿或還原對照表。
- 原始碼內容、路徑、PII 與 private map 均不會寫進報告。

| 檢查代號 | 結果 |
|---|---|
| `ui_app_class` | PASS |
| `ui_run_files` | PASS |
| `ui_background_start` | PASS |
| `ui_poll` | PASS |
| `ui_dispatch_files_call` | PASS |
| `ui_background_thread` | PASS |
| `ui_hotkey_lock` | PASS |
| `ui_done_queue` | PASS |
| `core_dispatch_files` | PASS |
| `core_media_entry_call` | NOT_FOUND |
| `core_media_exception_continues` | PASS |
| `handler_media_mask_pdf` | PASS |
| `handler_media_mask_image` | PASS |
| `handler_media_mask_file` | PASS |
| `handler_media_ocr_words` | PASS |
| `handler_media_backend_wrapper` | PASS |
| `ocr_backend_rapid_words` | PASS |
| `ocr_backend_wordbox_requested` | PASS |
| `ocr_backend_adapter_converter` | PASS |
| `v6676_benchmark_present` | PASS |
| `v6676_gpu_worker_present` | PASS |
| `v5_acceptance_present` | PASS |
| `synthetic_fixture_present` | PASS |
| `windows_env_present` | PASS |
| `windows_gpu_env_present` | PASS |

## 待確認項目

- 必要掛接點未確認：1 個。
- core 是否可能在媒體處理錯誤後 `continue`：True。
- `STATIC_HOOKS_READY_FOR_REVIEW` 不是 GPU 上線許可：仍需確認最新 `ui.py` / `core.py` 並製作隔離整合副本。
- 真正安全處理的驗收仍需重新進行 27/27、17/17、PDF 結構與 CPU/GPU 像素比較。
