# WIDER FACE 500｜YuNet 人臉偵測 Benchmark

資料集：WIDER FACE validation，seed=42，隨機固定抽樣 500 張圖片。

評估：原始 YuNet 偵測框，IoU ≥ 0.5，一對一配對；不計黑框 padding。

| 指標 | 結果 |
|---|---:|
| 有效 GT 人臉數 | 5438 |
| 忽略標註人臉數 | 117 |
| TP | 3179 |
| FP | 481 |
| FN | 2259 |
| Precision | 86.86% |
| Recall | 58.46% |
| F1 | 0.6988 |
| 平均純偵測耗時 | 0.072 秒／張 |
| 中位純偵測耗時 | 0.059 秒／張 |

**提醒：** 這是 500 張子集的固定 IoU/信心門檻 PRF1，不等於 WIDER FACE 官方 Easy/Medium/Hard AP；未驗證最終遮蔽像素覆蓋。
