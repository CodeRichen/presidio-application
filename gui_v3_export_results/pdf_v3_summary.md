# PDF 安全輸出 V3 分段效能診斷

測試次數：3，成功：3

| 步驟 | 呼叫數 | 累積秒數 | 平均 ms/次 | 最慢 ms/次 |
|---|---:|---:|---:|---:|
| page.encode_png | 21 | 9.755 | 464.54 | 655.30 |
| page.insert_image | 21 | 2.690 | 128.09 | 198.33 |
| page.new_page | 21 | 0.005 | 0.26 | 0.45 |
| page.render | 21 | 3.405 | 162.14 | 200.06 |
| pdf.atomic_replace | 3 | 0.014 | 4.73 | 6.20 |
| pdf.save | 3 | 10.055 | 3351.51 | 3581.58 |
| pdf.verify | 3 | 0.297 | 98.93 | 99.40 |

## 最慢單次呼叫

| 階段 | 頁數 | 耗時 ms |
|---|---:|---:|
| pdf.save | — | 3581.58 |
| pdf.save | — | 3434.72 |
| pdf.save | — | 3038.24 |
| page.encode_png | 6 | 655.30 |
| page.encode_png | 5 | 598.76 |
| page.encode_png | 6 | 575.37 |
| page.encode_png | 6 | 567.83 |
| page.encode_png | 5 | 519.65 |
| page.encode_png | 5 | 513.17 |
| page.encode_png | 4 | 503.06 |
| page.encode_png | 1 | 497.29 |
| page.encode_png | 1 | 481.06 |

**提醒**：這些測量不能證明 GIL 問題。PDF 安全輸出仍需維持完整結構與遮蔽驗證。
