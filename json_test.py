# -*- coding: utf-8 -*-
"""
測試程式_多版本評分.py（單篇文章版，三種寬鬆程度評分）
====================================================================
在原本「測試程式.py」的流程上，把評分部分擴充成三種版本：

    版本一（嚴格）    : 偵測範圍的起訖位置與答案完全一致，且類型(label)完全一致，才算 TP
    版本二（邊界寬鬆）: 偵測範圍只要跟答案範圍「有重疊」即可(不用切得剛好)，但類型要一致
    版本三（最寬鬆）  : 偵測範圍只要跟答案範圍「有重疊」即可，類型錯了也算 TP
                        （只看「這個位置有沒有被遮蔽到」，不管遮蔽對不對）

複製/貼上/還原三個步驟跟原本測試程式一樣，完全沒動 main.py / handler_*.py，
只是把「分析」那一段的比對邏輯換成三組。

用法跟原本一樣：跟 main.py / handler_text.py / handler_code.py 放同一個資料夾，
並把答案檔路徑改成你自己的 t1.json 位置。
"""
import os
import sys
import json
import time
import types
import traceback
import contextlib

TARGET_DIR = os.path.dirname(os.path.abspath(__file__))
ANSWER_FILE = os.path.join(TARGET_DIR, "presidio/t1.json")
RESULT_FILE = os.path.join(TARGET_DIR, "presidio/測試結果_多版本.txt")


@contextlib.contextmanager
def _silence_stdout():
    with open(os.devnull, "w", encoding="utf-8") as devnull, contextlib.redirect_stdout(devnull):
        yield


# ============================== 假模組區（僅取代 OS/GUI 邊界，邏輯不動）==============================
def _make_module(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _FakeClipboard:
    def __init__(self):
        self.text = ""
        self.files = None

    def set_text(self, text):
        self.text = text
        self.files = None


FAKE_CLIPBOARD = _FakeClipboard()

_make_module("win32clipboard",
    OpenClipboard=lambda *a, **k: None, CloseClipboard=lambda *a, **k: None,
    EmptyClipboard=lambda *a, **k: None,
    IsClipboardFormatAvailable=lambda fmt: FAKE_CLIPBOARD.files is not None,
    GetClipboardData=lambda fmt: FAKE_CLIPBOARD.files,
    SetClipboardData=lambda fmt, data: None, CF_HDROP=15)
_make_module("win32gui", GetForegroundWindow=lambda: 1, GetWindowText=lambda h: "MockWindow")
_make_module("win32process", GetWindowThreadProcessId=lambda h: (0, 1234))
_make_module("win32api", keybd_event=lambda *a, **k: None)
_make_module("win32con", VK_MENU=18, VK_CONTROL=17, KEYEVENTF_KEYUP=2)


class _FakeProcess:
    def __init__(self, pid): self.pid = pid
    def name(self): return "mock_process.exe"


_make_module("psutil", Process=_FakeProcess)
_make_module("pyperclip",
    copy=lambda text: FAKE_CLIPBOARD.set_text(text),
    paste=lambda: FAKE_CLIPBOARD.text)


class _FakeGlobalHotKeys:
    def __init__(self, mapping): self.mapping = mapping; self.running = False
    def start(self): self.running = True
    def stop(self): self.running = False


_make_module("pynput.keyboard", GlobalHotKeys=_FakeGlobalHotKeys)
_make_module("pynput", keyboard=sys.modules["pynput.keyboard"])
_pil_image_mod = _make_module("PIL.Image", Image=object)
_make_module("PIL.ImageDraw", ImageDraw=object)
_make_module("PIL", Image=_pil_image_mod, ImageDraw=sys.modules["PIL.ImageDraw"])


class _FakePytesseractOutput:
    DICT = "dict"


_make_module("pytesseract", pytesseract=types.SimpleNamespace(tesseract_cmd=""),
    image_to_data=lambda *a, **k: {}, Output=_FakePytesseractOutput)
_make_module("pymupdf")
# =======================================================================================================

sys.path.insert(0, TARGET_DIR)
try:
    import main  # noqa: E402  原封不動
except Exception:
    print("匯入 main.py 失敗，請確認這支程式跟 main.py / handler_*.py 放在同一個資料夾。")
    traceback.print_exc()
    sys.exit(1)


def detect_with_offsets(text):
    """跟 anonymize_text 完全相同的偵測邏輯，但保留 (start, end, 值, 標籤) 座標資訊。"""
    is_code = main.is_code_text(text)
    matches = main.handler_code.detect(text) if is_code else main.handler_text.detect(text)
    matches = main.merge_overlaps(matches)
    detected = [(s, e, text[s:e], label) for s, e, label, score in matches]
    return is_code, detected


def locate_expected(article, answer):
    """答案檔只有 (值, 標籤)，反查回文章中的實際位置，回傳 (start, end, 值, 標籤)。
    同一個值若重複出現，依序往後找，盡量對齊文章中出現的順序。"""
    located = []
    cursor_by_value = {}
    for value, label in answer:
        start_from = cursor_by_value.get(value, 0)
        idx = article.find(value, start_from)
        if idx == -1:
            idx = article.find(value)  # 保底：找不到就從頭找一次
        if idx == -1:
            located.append((None, None, value, label))  # 答案本身不在文章裡（不應發生，但保留可見性）
            continue
        cursor_by_value[value] = idx + 1
        located.append((idx, idx + len(value), value, label))
    return located


def _overlaps(a_start, a_end, b_start, b_end):
    if a_start is None or b_start is None:
        return False
    return a_start < b_end and b_start < a_end


def score_version(expected_located, detected, *, require_exact_span, require_label):
    """
    require_exact_span=True  -> 版本一：起訖位置完全一致
    require_exact_span=False -> 版本二/三：只要有重疊即可
    require_label=True       -> 類型要一致（版本一、二）
    require_label=False      -> 類型不看（版本三）
    回傳 (tp_pairs, fp_leftover, fn_leftover)
    """
    exp_left = list(expected_located)
    det_left = list(detected)
    tp_pairs = []

    for exp in list(exp_left):
        es, ee, ev, el = exp
        found = None
        for det in det_left:
            ds, de, dv, dl = det
            if require_exact_span:
                span_ok = (es == ds and ee == de)
            else:
                span_ok = _overlaps(es, ee, ds, de)
            label_ok = (dl == el) if require_label else True
            if span_ok and label_ok:
                found = det
                break
        if found:
            tp_pairs.append((exp, found))
            exp_left.remove(exp)
            det_left.remove(found)

    return tp_pairs, det_left, exp_left  # tp, fp(剩下沒配對到的偵測), fn(剩下沒配對到的答案)


def _prf(tp, fp, fn):
    precision = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
    recall = tp / (tp + fn) if (tp + fn) > 0 else float("nan")
    if precision == precision and recall == recall and (precision + recall) > 0:
        f1 = 2 * precision * recall / (precision + recall)
    else:
        f1 = float("nan")
    return precision, recall, f1


def _fmt_pct(x):
    return f"{x:.2%}" if x == x else "N/A"


def main_test():
    if not os.path.exists(ANSWER_FILE):
        print(f"找不到解答檔：{ANSWER_FILE}"); sys.exit(1)
    with open(ANSWER_FILE, encoding="utf-8") as f:
        data = json.load(f)
    article, answer = data["article"], data["answer"]

    mapping = {}
    lines = []
    def out(s=""): lines.append(s)

    out("=" * 78)
    out("剪貼簿敏感資訊防護 - 單篇文章 一次複製/貼上 測試報告（多版本評分）")
    out(f"文章長度：{len(article)} 字元　答案筆數：{len(answer)}")
    out("=" * 78)

    # ---- 步驟1：一次性複製整篇文章（觸發整批偵測+遮蔽） ----
    FAKE_CLIPBOARD.set_text(article)
    t0 = time.perf_counter()
    with _silence_stdout():
        main.on_copy(mapping)
    t1 = time.perf_counter()
    masked_text = FAKE_CLIPBOARD.text

    # ---- 步驟2：貼上（只模擬按鍵，不改剪貼簿內容） ----
    t2 = time.perf_counter()
    with _silence_stdout():
        main.on_paste(mapping)
    t3 = time.perf_counter()
    paste_unchanged = (FAKE_CLIPBOARD.text == masked_text)

    # ---- 步驟3：把遮蔽後的文章丟回 on_copy，驗證整篇還原 ----
    FAKE_CLIPBOARD.set_text(masked_text)
    t4 = time.perf_counter()
    with _silence_stdout():
        main.on_copy(mapping)
    t5 = time.perf_counter()
    restored_text = FAKE_CLIPBOARD.text
    restored_ok = (restored_text == article)

    out("")
    out(f"[複製] 整篇偵測+遮蔽耗時: {(t1-t0)*1000:.3f} ms")
    out(f"[貼上] 耗時: {(t3-t2)*1000:.3f} ms | 剪貼簿內容維持不變(符合 on_paste 設計): {'OK' if paste_unchanged else 'FAIL'}")
    out(f"[還原] 複製回來耗時: {(t5-t4)*1000:.3f} ms | 完全還原成原始文章: {'OK' if restored_ok else 'FAIL'}")
    if not restored_ok:
        out(f"  還原後長度 {len(restored_text)} vs 原始長度 {len(article)}")

    # ---- 步驟4：分析 —— 對原始文章單獨跑一次偵測，取得座標後做三種比對 ----
    is_code, detected = detect_with_offsets(article)
    expected_located = locate_expected(article, answer)
    out(f"[分類] 文章整體判斷: {'程式碼' if is_code else '一般文字'}")

    out("")
    out(f"解答共 {len(answer)} 筆: {answer}")
    out(f"實際偵測到 {len(detected)} 筆: {[(t, l) for _, _, t, l in detected]}")

    versions = [
        ("版本一：嚴格比對（起訖位置 + 類型完全一致）", dict(require_exact_span=True, require_label=True)),
        ("版本二：寬鬆比對（範圍有重疊即可，但類型需正確）", dict(require_exact_span=False, require_label=True)),
        ("版本三：最寬鬆比對（範圍有重疊即算對，不論類型）", dict(require_exact_span=False, require_label=False)),
    ]

    for title, kwargs in versions:
        tp_pairs, fp_left, fn_left = score_version(expected_located, detected, **kwargs)
        precision, recall, f1 = _prf(len(tp_pairs), len(fp_left), len(fn_left))

        out("")
        out("-" * 78)
        out(title)
        out("-" * 78)
        out(f"命中(TP): {len(tp_pairs)}  誤判(FP): {len(fp_left)}  漏判(FN): {len(fn_left)}")
        if fp_left:
            out(f"誤判內容: {[(t, l) for _, _, t, l in fp_left]}")
        if fn_left:
            out(f"漏判內容: {[(v, l) for _, _, v, l in fn_left]}")
        out(f"Precision: {_fmt_pct(precision)}")
        out(f"Recall   : {_fmt_pct(recall)}")
        out(f"F1 Score : {_fmt_pct(f1)}")
        out(f"結果: {'✔ 完全正確' if not fp_left and not fn_left else '✘ 有誤判或漏判'}")

    out("")
    out("=" * 78)

    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main_test()