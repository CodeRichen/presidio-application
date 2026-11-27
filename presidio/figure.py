import matplotlib.pyplot as plt
import numpy as np

# 評分數據
versions = [
    "Strict",
    "Relaxed",
    "Overlap only"
]

precision = [45.73, 52.44, 81.99]
recall = [47.17, 54.43, 83.54]
f1 = [46.44, 53.42, 82.76]

# 柔和配色
colors = {
    "Precision": "#A8C5DA",  # 霧藍
    "Recall": "#B8D8C0",     # 鼠尾草綠
    "F1-score": "#E8BFC4"    # 奶油粉
}

# 圖表設定
y = np.arange(len(versions))
height = 0.23

fig, ax = plt.subplots(figsize=(11, 6))

# 繪製橫向長條圖
bars1 = ax.barh(
    y - height, precision, height,
    label="Precision", color=colors["Precision"]
)

bars2 = ax.barh(
    y, recall, height,
    label="Recall", color=colors["Recall"]
)

bars3 = ax.barh(
    y + height, f1, height,
    label="F1-score", color=colors["F1-score"]
)

# 數值標示
for bars in [bars1, bars2, bars3]:
    for bar in bars:
        width = bar.get_width()
        ax.text(
            width + 1,
            bar.get_y() + bar.get_height() / 2,
            f"{width:.2f}%",
            va="center",
            fontsize=10,
            color="#555555"
        )



ax.set_xlabel("Score (%)", fontsize=11, color="#555555")
ax.set_yticks(y)
ax.set_yticklabels(versions, fontsize=11)

# 數值範圍
ax.set_xlim(0, 100)

# 讓 Version 1 顯示在最上方
ax.invert_yaxis()

# 圖例
ax.legend(
    loc="upper right",
    frameon=False,
    fontsize=10,
    ncol=3
)

# 柔和背景與網格
ax.set_facecolor("#FAFAF8")
fig.patch.set_facecolor("#FAFAF8")

ax.grid(
    axis="x",
    linestyle="",
    linewidth=0.8,
    alpha=0.25
)

ax.set_axisbelow(True)

# 移除多餘邊框
for spine in ax.spines.values():
    spine.set_visible(False)

ax.tick_params(axis="both", length=0, colors="#555555", bottom=False,labelbottom=False)

plt.tight_layout()

# 輸出圖片
plt.savefig(
    "pii_detection_comparison.png",
    dpi=300,
    bbox_inches="tight",
    facecolor=fig.get_facecolor()
)

plt.show()