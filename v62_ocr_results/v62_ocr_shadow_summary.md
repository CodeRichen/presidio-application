# V6.2 OCR 安全性影子模式報告

**狀態：SHADOW_SYNTHETIC_ONLY**

本測試只分析合成影像 PDF；所有頁面仍實際執行 2×+3×，節省時間為推估，不是實測加速。
**任何結果均不得用來直接部署跳過 3× 的策略於真實文件。**

| 資料組 | 頁數 | 獨立 GT 雙解析度 | 影子選擇性 | 估計略過 3× 次數 | 推估省時 |
|---|---:|---:|---:|---:|---:|
| hardcases | 8 | 30/30 | 30/30 | 3 | 30.23s |

## hardcases 逐頁結果

| 案例 | 雙解析度 GT | 選擇性 GT | 模擬跳過 3× | 3× 額外框 | 判定理由 |
|---|---:|---:|---|---:|---|
| large_clean | 3/3 | 3/3 | 是 | 0 | meets_experimental_quality_heuristic |
| small_text | 3/3 | 3/3 | 否 | 0 | low_contrast, low_edge_strength |
| tiny_text | 3/3 | 3/3 | 否 | 0 | small_or_missing_2x_words, low_contrast, low_edge_strength |
| low_contrast | 3/3 | 3/3 | 否 | 0 | low_contrast, low_edge_strength |
| blurred_scan | 3/3 | 3/3 | 否 | 0 | low_contrast, low_edge_strength |
| large_title_tiny_pii | 3/3 | 3/3 | 否 | 0 | small_or_missing_2x_words |
| mixed_sizes | 6/6 | 6/6 | 是 | 0 | meets_experimental_quality_heuristic |
| multi_records | 6/6 | 6/6 | 是 | 0 | meets_experimental_quality_heuristic |

**注意**：GT 只包含明確標註的合成個資；GT 命中率不能證明沒有未知個資。
只保存數量、座標覆蓋率和耗時，不保存 OCR 字詞或私有還原 map。
