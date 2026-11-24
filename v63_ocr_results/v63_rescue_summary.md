# V6.3：2× 漏判 / 3× 補救 — OCR 影子測試

**狀態：NO_CONFIRMED_3X_RESCUES**

每頁仍實際執行 2× 與 3× OCR，任何省時都是模擬估計；**不得部署真實文件**。
資料均為臨時生成的合成影像；標註於繪圖時獨立產生，報告不包含原始值、OCR 文字或影像。
使用 V6.2 原有的門檻（僅 2× 特徵做決策），未依此份 GT 自動調整門檻。

| 資料集 | 頁數 | 已標註 PII | 2× 命中 | 雙解析度命中 | 3× 補救數 | 危險跳過 | 估計略過頁 |
|---|---:|---:|---:|---:|---:|---:|---:|
| discovery | 16 | 64 | 64 | 64 | 0 | 0 | 10 |
| holdout | 8 | 32 | 32 | 32 | 0 | 0 | 5 |

## discovery 逐頁

| 合成情境 | 類型 | 2× / GT | 雙解析度 / GT | 3× 補救 | 跳過 3×？ | 危險跳過 |
|---|---|---:|---:|---:|---|---:|
| discovery_01_micro_email | micro_email | 4/4 | 4/4 | 0 | 否 | 0 |
| discovery_02_tiny_phone | tiny_phone | 4/4 | 4/4 | 0 | 否 | 0 |
| discovery_03_faint_email | faint_email | 4/4 | 4/4 | 0 | 是 | 0 |
| discovery_04_local_blur | local_blur | 4/4 | 4/4 | 0 | 是 | 0 |
| discovery_05_jpeg_artifacts | jpeg_artifacts | 4/4 | 4/4 | 0 | 是 | 0 |
| discovery_06_watermark | watermark | 4/4 | 4/4 | 0 | 是 | 0 |
| discovery_07_mixed_small | mixed_small | 4/4 | 4/4 | 0 | 是 | 0 |
| discovery_08_clean_control | clean_control | 4/4 | 4/4 | 0 | 是 | 0 |
| discovery_09_micro_email | micro_email | 4/4 | 4/4 | 0 | 否 | 0 |
| discovery_10_tiny_phone | tiny_phone | 4/4 | 4/4 | 0 | 否 | 0 |
| discovery_11_faint_email | faint_email | 4/4 | 4/4 | 0 | 是 | 0 |
| discovery_12_local_blur | local_blur | 4/4 | 4/4 | 0 | 否 | 0 |
| discovery_13_jpeg_artifacts | jpeg_artifacts | 4/4 | 4/4 | 0 | 是 | 0 |
| discovery_14_watermark | watermark | 4/4 | 4/4 | 0 | 否 | 0 |
| discovery_15_mixed_small | mixed_small | 4/4 | 4/4 | 0 | 是 | 0 |
| discovery_16_clean_control | clean_control | 4/4 | 4/4 | 0 | 是 | 0 |

## holdout 逐頁

| 合成情境 | 類型 | 2× / GT | 雙解析度 / GT | 3× 補救 | 跳過 3×？ | 危險跳過 |
|---|---|---:|---:|---:|---|---:|
| holdout_01_micro_email | micro_email | 4/4 | 4/4 | 0 | 否 | 0 |
| holdout_02_tiny_phone | tiny_phone | 4/4 | 4/4 | 0 | 否 | 0 |
| holdout_03_faint_email | faint_email | 4/4 | 4/4 | 0 | 是 | 0 |
| holdout_04_local_blur | local_blur | 4/4 | 4/4 | 0 | 是 | 0 |
| holdout_05_jpeg_artifacts | jpeg_artifacts | 4/4 | 4/4 | 0 | 是 | 0 |
| holdout_06_watermark | watermark | 4/4 | 4/4 | 0 | 是 | 0 |
| holdout_07_mixed_small | mixed_small | 4/4 | 4/4 | 0 | 否 | 0 |
| holdout_08_clean_control | clean_control | 4/4 | 4/4 | 0 | 是 | 0 |

## 判讀

- `UNSAFE_SKIP_FOUND`：有 2× 漏、雙解析度補救，但策略仍跳過 3× → **拒絕部署**。
- `BASELINE_GROUND_TRUTH_GAPS`：雙解析度也漏掉已知 PII → 先修 OCR 或資料。
- `NO_CONFIRMED_3X_RESCUES`：未找到真正的補救案例，不能驗證補救觸發能力。
- `DISCOVERY_RESCUES_ONLY`：只有探索資料有補救案例，保留獨立驗證缺口。
- `RESCUES_OBSERVED_SHADOW_ONLY`：探索與留出測試皆有補救，且未見新增漏判；仍非生產安全證明。
- `INCOMPLETE`：子程序失敗或逾時，不能得出結論。

**限制**：這些是固定合成版型下的留出種子，不是實際使用者文件的代表性抽樣。
GT 矩形 95% 覆蓋率只是框匹配代理指標，不是最終 PDF 的黑框覆蓋驗收。
面對真實 PII 仍需保留目前的 2×+3× 和 V5 安全輸出。
