# -*- coding: utf-8 -*-
"""
測試程式_多版本評分_檔案輸入版.py（放在 presidio 資料夾底下）
====================================================================
本檔案放置位置：  .../presidio/測試程式_多版本評分_檔案輸入版.py
main.py / handler_text.py / handler_code.py / handler_media.py 位置：
    .../ （presidio 的上一層）

執行時會用 input() 詢問要測試哪個檔案，可以輸入：
    - .json                        ：跟以前一樣，裡面同時有 "article" 跟 "answer"，
                                      走「文字/程式碼」流程：一次複製/貼上/還原三步驟
                                      + 三種寬鬆程度評分(位置需重疊或完全一致)。
    - .pdf / 圖片檔                ：**不自己重寫 OCR**，直接呼叫專案裡已經寫好的
                                      handler_media.mask_pdf() / mask_image()，
                                      用它回傳的 entries (value/label/score) 去跟
                                      答案比對。因為圖片/PDF 沒有像文字一樣的線性
                                      字元位置，所以比對邏輯改成「值有沒有重疊
                                      (互為子字串)」而不是「位置區間重疊」。
                                      這個分支只驗證「偵測有沒有準」，不驗證
                                      main.py 的複製/貼上流程(那段跟 win32 相關，
                                      跟 handler_media 本身的偵測準確度無關)。

                                      因為是 pdf/圖片，答案檔裡沒有 article，
                                      程式會再問一次「答案 json 檔名」，
                                      裡面只需要 "answer"，例如：
                                          { "answer": [["王小明", "PERSON"],
                                                       ["0912345678", "PHONE"]] }

輸入檔名時，只打檔名(不含路徑)會預設去這支程式所在的資料夾(presidio)裡找，
也可以直接貼絕對路徑。

pdf/圖片分支會直接 import 專案本身的 handler_media.py，所以請確保該檔頭說明
的套件(pillow / pytesseract / pymupdf + Tesseract-OCR 主程式)都已經裝好。
"""
import os
import sys
import json
import time
import types
import tempfile
import traceback
import contextlib
import sys
for name in ["main", "handler_text", "handler_code", "handler_media"]:
    sys.modules.pop(name, None)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # presidio 資料夾
PARENT_DIR = os.path.dirname(SCRIPT_DIR)                  # main.py / handler_*.py 所在的上一層資料夾

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif", ".webp"}
PDF_EXTS = {".pdf"}

sys.path.insert(0, PARENT_DIR)


@contextlib.contextmanager
def _silence_stdout():
    with open(os.devnull, "w", encoding="utf-8") as devnull, contextlib.redirect_stdout(devnull):
        yield


def _resolve_path(filename):
    """允許輸入絕對路徑，也允許只打檔名(預設去 presidio 資料夾底下找)。"""
    filename = filename.strip().strip('"').strip("'")
    if os.path.isabs(filename) and os.path.exists(filename):
        return filename
    candidate = os.path.join(SCRIPT_DIR, filename)
    if os.path.exists(candidate):
        return candidate
    if os.path.exists(filename):
        return os.path.abspath(filename)
    return None


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


# ============================== 分支一：json（文字/程式碼）==============================
def run_text_flow(article, answer, result_file):
    """跟原本「測試程式_多版本評分.py」完全一樣的流程：只是把輸出檔路徑改成參數傳入。"""

    # ---------- 假模組區（僅取代 OS/GUI 邊界，main.py 的邏輯不動）----------
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
    # 注意：這裡刻意「不」假造 PIL / pytesseract / pymupdf，
    # 因為 main.py 會 import handler_media，而 handler_media 需要真的 PIL /
    # pytesseract / pymupdf 才能運作；文字/程式碼流程用不到這三個套件的功能，
    # 讓它們保持真的即可，不會影響這個分支的測試。
    # ------------------------------------------------------------------------

    try:
        import main  # noqa: E402  原封不動
    except Exception:
        print("匯入 main.py 失敗，請確認 main.py / handler_*.py 是否放在 presidio 的上一層資料夾。")
        traceback.print_exc()
        sys.exit(1)

    # ---- 暖機：先觸發一次偵測，把 NER 模型(以及可能的 registry fallback)載入完成，
    #      這樣才不會把「模型第一次載入」的時間算進下面的正式計時。這裡故意不靜音，
    #      讓你看得到載入過程有沒有跑完。----
    print("\n[暖機] 觸發模型載入(僅第一次需要，不計入下方計時)...")
    try:
        _warmup_text = "暖機用測試文字 test123 test@example.com"
        (main.handler_code if main.is_code_text(_warmup_text) else main.handler_text).detect(_warmup_text)
    except Exception:
        traceback.print_exc()
    print("[暖機] 完成，開始正式測試\n")

    def detect_with_offsets(text):
        is_code = main.is_code_text(text)
        matches = main.handler_code.detect(text) if is_code else main.handler_text.detect(text)
        matches = main.merge_overlaps(matches)
        detected = [(s, e, text[s:e], label) for s, e, label, score in matches]
        return is_code, detected

    def locate_expected(article, answer):
        located = []
        cursor_by_value = {}
        for value, label in answer:
            start_from = cursor_by_value.get(value, 0)
            idx = article.find(value, start_from)
            if idx == -1:
                idx = article.find(value)
            if idx == -1:
                located.append((None, None, value, label))
                continue
            cursor_by_value[value] = idx + 1
            located.append((idx, idx + len(value), value, label))
        return located

    def _overlaps(a_start, a_end, b_start, b_end):
        if a_start is None or b_start is None:
            return False
        return a_start < b_end and b_start < a_end

    def score_version(expected_located, detected, *, require_exact_span, require_label):
        exp_left = list(expected_located)
        det_left = list(detected)
        tp_pairs = []
        for exp in list(exp_left):
            es, ee, ev, el = exp
            found = None
            for det in det_left:
                ds, de, dv, dl = det
                span_ok = (es == ds and ee == de) if require_exact_span else _overlaps(es, ee, ds, de)
                label_ok = (dl == el) if require_label else True
                if span_ok and label_ok:
                    found = det
                    break
            if found:
                tp_pairs.append((exp, found))
                exp_left.remove(exp)
                det_left.remove(found)
        return tp_pairs, det_left, exp_left

    mapping = {}
    lines = []
    def out(s=""): lines.append(s)

    out("=" * 78)
    out("剪貼簿敏感資訊防護 - 單篇文章 一次複製/貼上 測試報告（多版本評分）")
    out(f"文章長度：{len(article)} 字元　答案筆數：{len(answer)}")
    out("=" * 78)

    FAKE_CLIPBOARD.set_text(article)
    t0 = time.perf_counter()
    with _silence_stdout():
        main.on_copy(mapping)
    t1 = time.perf_counter()
    masked_text = FAKE_CLIPBOARD.text

    t2 = time.perf_counter()
    with _silence_stdout():
        main.on_paste(mapping)
    t3 = time.perf_counter()
    paste_unchanged = (FAKE_CLIPBOARD.text == masked_text)

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

    with open(result_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"報告已寫入：{result_file}")


# ============================== 分支二：pdf / 圖片 ==============================
def _value_overlap(a, b):
    """圖片/PDF 沒有線性字元位置可比對，改用「值互為子字串」當作等同於位置重疊。"""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    return a in b or b in a


def score_media(expected, detected, *, require_exact_value, require_label):
    """
    require_exact_value=True  -> 版本一：值完全一致
    require_exact_value=False -> 版本二/三：值有重疊(互為子字串)即可
    require_label=True        -> 版本一、二：類型要一致
    require_label=False       -> 版本三：類型不看
    """
    exp_left = list(expected)
    det_left = list(detected)
    tp_pairs = []
    for exp in list(exp_left):
        ev, el = exp
        found = None
        for det in det_left:
            dv, dl = det
            value_ok = (ev.strip() == dv.strip()) if require_exact_value else _value_overlap(ev, dv)
            label_ok = (dl == el) if require_label else True
            if value_ok and label_ok:
                found = det
                break
        if found:
            tp_pairs.append((exp, found))
            exp_left.remove(exp)
            det_left.remove(found)
    return tp_pairs, det_left, exp_left


def run_media_flow(path, answer, result_file):
    """直接呼叫專案裡已經寫好的 handler_media.mask_pdf / mask_image，不自己重寫 OCR。"""
    try:
        import handler_media
    except Exception:
        print("匯入 handler_media.py 失敗，請確認它跟 main.py 放在同一個資料夾(presidio 的上一層)，"
              "且已安裝 pillow / pytesseract / pymupdf 與 Tesseract-OCR 主程式。")
        traceback.print_exc()
        sys.exit(1)

    ext = os.path.splitext(path)[1].lower()
    stem = os.path.splitext(os.path.basename(path))[0]

    # 輸出檔跟對照表都丟到獨立的暫存資料夾，不動到正式環境的 handler_media.DEFAULT_MAP_FILE
    tmp_dir = tempfile.mkdtemp(prefix="presidio_media_test_")
    output_path = os.path.join(tmp_dir, f"{stem}_masked{ext}")
    map_file = os.path.join(tmp_dir, "media_test_map.json")

    print(f"呼叫 handler_media 進行偵測 + 遮蔽... (輸出: {output_path})")

    # ---- 暖機：先觸發一次偵測，把 NER 模型載入完成，避免第一次載入時間被算進下面的計時。----
    print("[暖機] 觸發模型載入(僅第一次需要，不計入下方計時)...")
    try:
        handler_media.detect_matches("暖機用測試文字 test123 test@example.com")
    except Exception:
        traceback.print_exc()
    print("[暖機] 完成，開始正式測試")

    t0 = time.perf_counter()
    if ext in PDF_EXTS:
        entries = handler_media.mask_pdf(path, output_path, map_file)
    else:
        entries = handler_media.mask_image(path, output_path, map_file)
    t1 = time.perf_counter()

    detected = [((e.get("value") or "").strip(), e["label"]) for e in entries]
    expected = [(str(v).strip(), lbl) for v, lbl in answer]

    lines = []
    def out(s=""): lines.append(s)

    out("=" * 78)
    out("剪貼簿敏感資訊防護 - 圖片/PDF 偵測測試報告（多版本評分，透過 handler_media）")
    out(f"輸入檔案：{path}")
    out(f"答案筆數：{len(expected)}")
    out("=" * 78)
    out("")
    out(f"[偵測+遮蔽] handler_media.{'mask_pdf' if ext in PDF_EXTS else 'mask_image'} 耗時: {(t1-t0)*1000:.3f} ms")
    out("")
    out(f"解答共 {len(expected)} 筆: {expected}")
    out(f"實際偵測到 {len(detected)} 筆: {detected}")

    versions = [
        ("版本一：嚴格比對（值完全一致 + 類型完全一致）", dict(require_exact_value=True, require_label=True)),
        ("版本二：寬鬆比對（值有重疊即可，但類型需正確）", dict(require_exact_value=False, require_label=True)),
        ("版本三：最寬鬆比對（值有重疊即算對，不論類型）", dict(require_exact_value=False, require_label=False)),
    ]
    for title, kwargs in versions:
        tp_pairs, fp_left, fn_left = score_media(expected, detected, **kwargs)
        precision, recall, f1 = _prf(len(tp_pairs), len(fp_left), len(fn_left))
        out("")
        out("-" * 78)
        out(title)
        out("-" * 78)
        out(f"命中(TP): {len(tp_pairs)}  誤判(FP): {len(fp_left)}  漏判(FN): {len(fn_left)}")
        if fp_left:
            out(f"誤判內容: {fp_left}")
        if fn_left:
            out(f"漏判內容: {fn_left}")
        out(f"Precision: {_fmt_pct(precision)}")
        out(f"Recall   : {_fmt_pct(recall)}")
        out(f"F1 Score : {_fmt_pct(f1)}")
        out(f"結果: {'✔ 完全正確' if not fp_left and not fn_left else '✘ 有誤判或漏判'}")

    out("")
    out(f"(遮蔽後檔案與對照表存在暫存資料夾，僅供檢查，不影響正式環境: {tmp_dir})")
    out("=" * 78)

    with open(result_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"報告已寫入：{result_file}")


# ============================== 進入點 ==============================
def main_test():
    raw = input("請輸入要測試的檔案名稱（json / pdf / 圖片檔，可直接打檔名或完整路徑）：")
    path = _resolve_path(raw)
    if path is None:
        print(f"找不到檔案：{raw}")
        sys.exit(1)

    ext = os.path.splitext(path)[1].lower()
    stem = os.path.splitext(os.path.basename(path))[0]
    result_file = os.path.join(SCRIPT_DIR, f"{stem}_result.txt")

    if ext == ".json":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        article = data["article"]
        answer = data["answer"]
        run_text_flow(article, answer, result_file)

    elif ext in PDF_EXTS or ext in IMAGE_EXTS:
        ans_raw = input("請輸入對應的答案 json 檔名（裡面只需要 answer，不用 article）：")
        ans_path = _resolve_path(ans_raw)
        if ans_path is None:
            print(f"找不到答案檔：{ans_raw}")
            sys.exit(1)
        with open(ans_path, encoding="utf-8") as f:
            ans_data = json.load(f)
        answer = ans_data["answer"]
        run_media_flow(path, answer, result_file)

    else:
        print(f"不支援的檔案類型：{ext}")
        sys.exit(1)


if __name__ == "__main__":
    main_test()