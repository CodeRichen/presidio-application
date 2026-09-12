import json
import argparse
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# 變數一：偵測系統跑出來的結果 (detection result)
#   格式: [(擷取到的文字, 標記的類別), ...]
#   → 換成你自己模型/系統的輸出即可
# ---------------------------------------------------------------------------
DETECTION_RESULT = [('地址高雄市苓雅區四維三路120號8樓', 'ADDRESS'), ('D9988771', 'CREDENTIAL_LIKE'), ('TR3345678', 'CREDENTIAL_LIKE'), ('2026年08月30日', 'DATE_OF_BIRTH'), ('https://vpn-internal-fake-corp.example.com/login），該時段地下二樓監視器編號', 'URL'), ('0x8f3CBe8B1a1A6e6E7c1e9E6D2f1B4a3C5d6E7F80', 'GENERIC_SECRET')]


# ---------------------------------------------------------------------------
# 變數二：正確答案 (ground truth) 資料檔路徑 —— 內含 article 與 answer
# ---------------------------------------------------------------------------
GROUND_TRUTH_FILE = "presidio/t2.json"

OUTPUT_IMAGE = "report/pii_highlight_result.png"

# ---------------------------------------------------------------------------
# 顏色設定
# ---------------------------------------------------------------------------
COLOR_CORRECT = (144, 238, 144)       # 綠：標對了
COLOR_MISSED = (255, 120, 120)        # 紅：漏標（洩露）
COLOR_WRONG_LABEL = (255, 221, 89)    # 黃：標到但類別錯
COLOR_FALSE_POS = (135, 206, 250)     # 藍：誤判（標了但其實不是敏感資訊）

LEGEND = [
    (COLOR_CORRECT, "正確偵測（有標記，類別正確）"),
    (COLOR_MISSED, "漏標／洩露（正確答案有，但沒被偵測到）"),
    (COLOR_WRONG_LABEL, "標記到，但類別標錯"),
    (COLOR_FALSE_POS, "誤判（偵測系統標了，但不是敏感資訊）"),
]

# 如果你的電腦上找不到下面任何一個字型，圖片文字就會變成方框/亂碼。
# 最保險的做法：直接把 CUSTOM_FONT_PATH 指到你電腦上一個支援中文的字型檔，
# 或是執行時加上 --font "字型檔路徑"。
CUSTOM_FONT_PATH = None  # 例如 Windows: r"C:\Windows\Fonts\msjh.ttc"

FONT_CANDIDATES = [
    # Windows 內建中文字型
    r"C:\Windows\Fonts\msjh.ttc",      # 微軟正黑體 (繁中，建議優先)
    r"C:\Windows\Fonts\msjhbd.ttc",
    r"C:\Windows\Fonts\mingliu.ttc",   # 細明體
    r"C:\Windows\Fonts\simsun.ttc",    # 新細明體/宋體 (簡中)
    r"C:\Windows\Fonts\msyh.ttc",      # 微軟雅黑 (簡中)
    r"C:\Windows\Fonts\simhei.ttf",
    # macOS 內建中文字型
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux (Noto CJK)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]


def load_ground_truth(path):
    """讀取含 article / answer 的 txt 或 json 檔"""
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    article = data["article"]
    answer = [tuple(item) for item in data["answer"]]
    return article, answer


def locate_spans(article, items):
    """
    在 article 裡依序找出每個 (text, label) 的字元區間 (start, end)。
    用「往後找」的方式搜尋，處理同一段文字重複出現的狀況；
    找不到的項目會回傳在 unresolved 清單裡，並印出警告。
    """
    spans = []
    unresolved = []
    cursor = 0
    for text, label in items:
        if not text:
            unresolved.append((text, label))
            continue
        idx = article.find(text, cursor)
        if idx == -1:
            idx = article.find(text)  # 找不到就從頭再找一次
        if idx == -1:
            unresolved.append((text, label))
            continue
        end = idx + len(text)
        spans.append((idx, end, label))
        cursor = end
    return spans, unresolved


def build_char_colors(article, gt_spans, det_spans):
    """逐字元判斷顏色"""
    n = len(article)
    gt_label = [None] * n
    det_label = [None] * n

    for start, end, label in gt_spans:
        for i in range(start, min(end, n)):
            gt_label[i] = label

    for start, end, label in det_spans:
        for i in range(start, min(end, n)):
            det_label[i] = label

    colors = [None] * n
    for i in range(n):
        g, d = gt_label[i], det_label[i]
        if g is not None and d is not None:
            colors[i] = COLOR_CORRECT if g == d else COLOR_WRONG_LABEL
        elif g is not None and d is None:
            colors[i] = COLOR_MISSED
        elif g is None and d is not None:
            colors[i] = COLOR_FALSE_POS
    return colors


def load_font(size, font_path=None):
    """
    載入支援中文的字型。優先順序：
      1) 呼叫時傳入的 font_path（例如 --font 參數）
      2) CUSTOM_FONT_PATH 變數
      3) FONT_CANDIDATES 清單裡第一個存在的路徑
    如果都找不到，會直接報錯（而不是默默換成沒有中文字的預設字型，
    那樣畫出來的中文就會變成方框/亂碼）。
    """
    candidates = [p for p in [font_path, CUSTOM_FONT_PATH] if p] + FONT_CANDIDATES
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size, index=0)
            except OSError:
                continue
    raise FileNotFoundError(
        "找不到可用的中文字型！請做以下其中一件事：\n"
        "  1) 把腳本裡的 CUSTOM_FONT_PATH 改成你電腦上任一個中文字型檔路徑\n"
        "     (Windows 範例: C:\\Windows\\Fonts\\msjh.ttc)\n"
        "  2) 執行時加上參數: python pii_highlight_eval.py --font \"字型檔路徑\"\n"
        f"目前嘗試過的路徑都不存在：{candidates}"
    )


def wrap_into_lines(article, colors, font, max_width):
    """把整篇文章依可視寬度換行，並保留原文中的換行符號"""
    lines = []
    current = []
    x = 0
    for i, ch in enumerate(article):
        if ch == "\n":
            lines.append(current)
            current = []
            x = 0
            continue
        w = font.getlength(ch)
        if current and x + w > max_width:
            lines.append(current)
            current = []
            x = 0
        current.append((ch, colors[i]))
        x += w
    if current:
        lines.append(current)
    return lines


def render_image(lines, font, legend, output_path, max_width,
                  padding=24, line_gap=6, unresolved_count=0):
    line_height = int(font.size * 1.6)
    legend_swatch = 22
    legend_line_h = 30
    legend_height = legend_swatch + len(legend) * legend_line_h + 50
    if unresolved_count:
        legend_height += 26

    img_w = max_width + padding * 2
    img_h = padding * 2 + len(lines) * (line_height + line_gap) + legend_height

    img = Image.new("RGB", (img_w, img_h), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    y = padding
    for line in lines:
        x = padding
        for ch, color in line:
            w = font.getlength(ch)
            if color is not None:
                draw.rectangle(
                    [x - 1, y - 2, x + w + 1, y + line_height - 4],
                    fill=color,
                )
            draw.text((x, y), ch, font=font, fill=(20, 20, 20))
            x += w
        y += line_height + line_gap

    # ---- legend ----
    y += 20
    title_font = load_font(int(font.size * 0.95), font_path=font.path if hasattr(font, "path") else None)
    draw.text((padding, y), "圖例說明 (Legend)：", font=title_font, fill=(0, 0, 0))
    y += legend_swatch + 14
    for color, desc in legend:
        draw.rectangle([padding, y, padding + legend_swatch, y + legend_swatch], fill=color, outline=(80, 80, 80))
        draw.text((padding + legend_swatch + 10, y + 2), desc, font=title_font, fill=(30, 30, 30))
        y += legend_line_h

    if unresolved_count:
        note_font = load_font(int(font.size * 0.85), font_path=font.path if hasattr(font, "path") else None)
        draw.text(
            (padding, y + 6),
            f"⚠ 有 {unresolved_count} 筆項目在原文中找不到對應文字，未納入標色（可能是文字被改寫/合併）。",
            font=note_font,
            fill=(150, 60, 60),
        )

    img.save(output_path)
    return output_path


def main():
    parser = argparse.ArgumentParser(description="PII 偵測結果視覺化比對工具")
    parser.add_argument("--data", default=GROUND_TRUTH_FILE, help="含 article/answer 的 txt 或 json 檔路徑")
    parser.add_argument("--output", default=OUTPUT_IMAGE, help="輸出圖片路徑")
    parser.add_argument("--font-size", type=int, default=22)
    parser.add_argument("--max-width", type=int, default=1000, help="每行文字最大像素寬度")
    parser.add_argument("--font", default=None, help="中文字型檔路徑（找不到字型/畫面亂碼時用這個指定）")
    args = parser.parse_args()

    article, ground_truth = load_ground_truth(args.data)
    detection_result = DETECTION_RESULT

    gt_spans, gt_unresolved = locate_spans(article, ground_truth)
    det_spans, det_unresolved = locate_spans(article, detection_result)

    if gt_unresolved:
        print(f"[警告] {len(gt_unresolved)} 筆正確答案在原文中找不到，已略過：")
        for text, label in gt_unresolved:
            print(f"    - ({label}) {text[:40]}")
    if det_unresolved:
        print(f"[警告] {len(det_unresolved)} 筆偵測結果在原文中找不到，已略過：")
        for text, label in det_unresolved:
            print(f"    - ({label}) {text[:40]}")

    colors = build_char_colors(article, gt_spans, det_spans)
    font = load_font(args.font_size, font_path=args.font)
    lines = wrap_into_lines(article, colors, font, args.max_width)

    out = render_image(
        lines, font, LEGEND, args.output, args.max_width,
        unresolved_count=len(gt_unresolved) + len(det_unresolved),
    )
    print(f"已輸出圖片：{out}")


if __name__ == "__main__":
    main()