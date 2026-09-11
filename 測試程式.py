# -*- coding: utf-8 -*-
"""
測試程式.py
====================================================================
目的：
    直接呼叫「原封不動」的 main.py / handler_text.py / handler_code.py，
    針對 敏感資料和解答.txt 裡的每個案例，跑一次完整的
        複製(偵測+遮蔽) -> 貼上(還原+重新遮蔽)
    流程，記錄各階段耗時，並把偵測結果跟解答檔逐項比對，
    計算命中(TP)、誤判(FP，多抓/抓錯)、漏判(FN，該抓的沒抓到)。

重要說明（請先讀）：
    main.py 本身是設計成監聽「真實鍵盤快速鍵」+「真實 Windows 剪貼簿」的常駐程式
    (win32clipboard / win32gui / win32api / pynput / psutil)。這些東西：
        1) 只能在 Windows + 有實體鍵盤事件的環境動作，無法在自動化測試中穩定重現
        2) 就算能動，用真的按鍵/剪貼簿去驅動測試也會有時間不穩定、環境依賴等問題，
           不適合拿來當「正確率」的量測基準
    所以這支測試程式的作法是：
        - 把 win32clipboard / win32gui / win32process / win32api / win32con /
          pynput / pyperclip / psutil / PIL / pytesseract / pymupdf(fitz)
          這些「作業系統 I/O 邊界」的模組全部換成假模組(mock)，
          剪貼簿改用一個記憶體變數模擬
        - main.py 裡真正的邏輯 (on_copy / on_paste / anonymize_text /
          deanonymize_text / handler_text.detect / handler_code.detect ...)
          完全沒有被修改，是原封不動被呼叫、真的跑過一次
    也就是說：這是「邏輯層級的完整流程模擬」，不是「真的去按鍵盤」。
    如果你要在你自己的 Windows 機器上驗證真實按鍵/剪貼簿行為，
    仍需要手動測試 main.py 本身；這支程式驗證的是「偵測規則的正確率」與
    「複製->貼上這段程式邏輯本身的執行時間與正確性」。

使用方式：
    1) 把這個檔案跟 敏感資料和解答.txt 放在跟 main.py / handler_*.py 同一個資料夾
    2) 直接執行：python 測試程式.py
    3) 結果會印在畫面上，同時寫進 測試結果.txt
"""
import os
import re
import sys
import time
import types
import traceback

# ============================== 路徑設定 ==============================
TARGET_DIR = os.path.dirname(os.path.abspath(__file__))  # main.py 等檔案所在資料夾
ANSWER_FILE = os.path.join(TARGET_DIR, "presidio/敏感資料和解答.txt")
RESULT_FILE = os.path.join(TARGET_DIR, "presidio/測試結果.txt")

# =======================================================================


# ============================== 假模組區 ==============================
# 把會用到真實作業系統/GUI/第三方模型的模組全部換成假的，
# 這樣 main.py 才能在任何環境下被 import 並且用可控、可重現的方式測試。
def _make_module(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# ---- 模擬剪貼簿：用一個記憶體變數取代真正的 Windows 剪貼簿 ----
class _FakeClipboard:
    def __init__(self):
        self.text = ""
        self.files = None  # 若要模擬「剪貼簿裡是檔案」，設成路徑 list
        self.history = []  # 記錄每一次剪貼簿內容變化，方便驗證流程

    def set_text(self, text):
        self.text = text
        self.files = None
        self.history.append(("TEXT", text))

    def set_files(self, paths):
        self.files = list(paths)
        self.history.append(("FILES", list(paths)))


FAKE_CLIPBOARD = _FakeClipboard()

_make_module(
    "win32clipboard",
    OpenClipboard=lambda *a, **k: None,
    CloseClipboard=lambda *a, **k: None,
    EmptyClipboard=lambda *a, **k: None,
    IsClipboardFormatAvailable=lambda fmt: FAKE_CLIPBOARD.files is not None,
    GetClipboardData=lambda fmt: FAKE_CLIPBOARD.files,
    SetClipboardData=lambda fmt, data: None,
    CF_HDROP=15,
)
_make_module(
    "win32gui",
    GetForegroundWindow=lambda: 1,
    GetWindowText=lambda h: "MockWindow",
)
_make_module(
    "win32process",
    GetWindowThreadProcessId=lambda h: (0, 1234),
)
_make_module(
    "win32api",
    keybd_event=lambda *a, **k: None,
)
_make_module(
    "win32con",
    VK_MENU=18,
    VK_CONTROL=17,
    KEYEVENTF_KEYUP=2,
)


class _FakeProcess:
    def __init__(self, pid):
        self.pid = pid

    def name(self):
        return "mock_process.exe"


_make_module("psutil", Process=_FakeProcess)


def _pyperclip_copy(text):
    FAKE_CLIPBOARD.set_text(text)


def _pyperclip_paste():
    return FAKE_CLIPBOARD.text


_make_module("pyperclip", copy=_pyperclip_copy, paste=_pyperclip_paste)


class _FakeGlobalHotKeys:
    def __init__(self, mapping):
        self.mapping = mapping
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


_keyboard_mod = _make_module("pynput.keyboard", GlobalHotKeys=_FakeGlobalHotKeys)
_make_module("pynput", keyboard=_keyboard_mod)

# ---- handler_media 的重型依賴 (PIL / pytesseract / pymupdf)：只需要能被 import，
#      本測試不涉及圖片/PDF 內容，所以用最小假模組頂替即可 ----
_pil_image_mod = _make_module("PIL.Image", Image=object)
_make_module("PIL.ImageDraw", ImageDraw=object)
_make_module("PIL", Image=_pil_image_mod, ImageDraw=sys.modules["PIL.ImageDraw"])


class _FakePytesseractOutput:
    DICT = "dict"


_make_module(
    "pytesseract",
    pytesseract=types.SimpleNamespace(tesseract_cmd=""),
    image_to_data=lambda *a, **k: {},
    Output=_FakePytesseractOutput,
)
_make_module("pymupdf")
# =======================================================================


sys.path.insert(0, TARGET_DIR)
try:
    import main  # noqa: E402  (真正要測試的程式，完全沒被修改)
except Exception:
    print("匯入 main.py 失敗，請確認這支測試程式跟 main.py / handler_*.py 放在同一個資料夾。")
    traceback.print_exc()
    sys.exit(1)


# ============================== 解答檔解析 ==============================
CASE_RE = re.compile(
    r"=== CASE:\s*(?P<id>.*?)\s*\|\s*(?P<desc>.*?)\s*===\s*\n"
    r"\[INPUT\]\n(?P<input>.*?)\n\[INPUT_END\]\s*\n"
    r"\[ANSWER\]\n(?P<answer>.*?)\[ANSWER_END\]",
    re.S,
)


def load_cases(path):
    with open(path, encoding="utf-8") as f:
        content = f.read()
    cases = []
    for m in CASE_RE.finditer(content):
        answer_lines = [ln for ln in m.group("answer").split("\n") if ln.strip()]
        answer_pairs = []
        for ln in answer_lines:
            if "\t" not in ln:
                continue
            value, label = ln.rsplit("\t", 1)
            answer_pairs.append((value, label.strip()))
        cases.append({
            "id": m.group("id").strip(),
            "desc": m.group("desc").strip(),
            "input": m.group("input"),
            "answer": answer_pairs,
        })
    return cases


# ============================== 比對邏輯 ==============================
def detect_raw(text):
    """直接呼叫跟 main.anonymize_text 完全相同的分類+偵測邏輯，取得 (value, label) 清單，
    不透過 mapping/tag，避免案例之間互相污染計數器，讓每個案例的偵測結果獨立可比對。"""
    is_code = main.is_code_text(text)
    matches = main.handler_code.detect(text) if is_code else main.handler_text.detect(text)
    matches = main.merge_overlaps(matches)
    detected = [(text[start:end], label) for start, end, label, score in matches]
    return is_code, detected


def score_case(expected, detected):
    """expected / detected 都是 (value, label) 的 list，允許同一案例有多筆。
    以「值+標籤」為單位做多重集合比對，回傳 tp / fp / fn 三個清單。"""
    exp_left = list(expected)
    det_left = list(detected)
    tp = []
    for pair in list(det_left):
        if pair in exp_left:
            tp.append(pair)
            exp_left.remove(pair)
            det_left.remove(pair)
    fp = det_left       # 偵測到了，但不在解答裡 -> 誤判
    fn = exp_left        # 解答裡有，但沒偵測到 -> 漏判
    return tp, fp, fn


# ============================== 完整複製->貼上流程模擬 ==============================
def run_full_copy_paste_cycle(text, mapping):
    """實際呼叫 main.on_copy() / main.on_paste()（原封不動的程式邏輯），
    模擬「使用者複製一段文字 -> 觸發快速鍵 -> 之後又按貼上」的完整流程，
    回傳各階段耗時與剪貼簿前後內容，用來驗證流程本身跑得動、跑得對、跑多快。"""
    FAKE_CLIPBOARD.set_text(text)

    t0 = time.perf_counter()
    main.on_copy(mapping)
    t1 = time.perf_counter()
    after_copy = FAKE_CLIPBOARD.text

    main.on_paste(mapping)
    t2 = time.perf_counter()
    after_paste = FAKE_CLIPBOARD.text  # on_paste 結束後應該改回遮蔽版本

    # 還原後應該要能拿回原始內容，驗證 map 存取/還原是否正確
    restored_ok = main.deanonymize_text(after_paste, mapping) == text

    return {
        "copy_ms": (t1 - t0) * 1000,
        "paste_ms": (t2 - t1) * 1000,
        "after_copy": after_copy,
        "after_paste": after_paste,
        "restored_ok": restored_ok,
    }


# ============================== 主流程 ==============================
def main_test():
    if not os.path.exists(ANSWER_FILE):
        print(f"找不到解答檔：{ANSWER_FILE}")
        sys.exit(1)

    cases = load_cases(ANSWER_FILE)
    if not cases:
        print("解答檔沒有解析到任何案例，請確認格式。")
        sys.exit(1)

    session_mapping = {}  # 模擬真實使用情境：整個測試過程共用同一份對照表
    lines = []
    total_tp = total_fp = total_fn = 0
    exact_match_count = 0
    copy_times, paste_times = [], []

    def out(s=""):
        print(s)
        lines.append(s)

    out("=" * 78)
    out("剪貼簿敏感資訊防護 - 偵測正確率 / 執行流程測試報告")
    out(f"測試案例數：{len(cases)}")
    out("=" * 78)

    for case in cases:
        text = case["input"]
        expected = case["answer"]

        is_code, detected = detect_raw(text)
        tp, fp, fn = score_case(expected, detected)

        cycle = run_full_copy_paste_cycle(text, session_mapping)

        total_tp += len(tp)
        total_fp += len(fp)
        total_fn += len(fn)
        copy_times.append(cycle["copy_ms"])
        paste_times.append(cycle["paste_ms"])
        is_exact = (not fp) and (not fn)
        if is_exact:
            exact_match_count += 1

        out("")
        out(f"--- CASE {case['id']} | {case['desc']} ---")
        out(f"分類判斷: {'程式碼' if is_code else '一般文字'}")
        out(f"解答應偵測: {expected if expected else '(無，應為乾淨文字)'}")
        out(f"實際偵測到: {detected if detected else '(無)'}")
        out(f"命中(TP): {len(tp)}  誤判(FP): {len(fp)}  漏判(FN): {len(fn)}"
            + (f"  | 誤判內容: {fp}" if fp else "")
            + (f"  | 漏判內容: {fn}" if fn else ""))
        out(f"本案例結果: {'✔ 完全正確' if is_exact else '✘ 有誤判或漏判'}")
        out(f"複製(偵測+遮蔽)耗時: {cycle['copy_ms']:.3f} ms | "
            f"貼上(還原+重新遮蔽)耗時: {cycle['paste_ms']:.3f} ms")
        out(f"還原正確性(貼上後能otherwise還原回原文): {'OK' if cycle['restored_ok'] else 'FAIL'}")

    precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else float("nan")
    recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else float("nan")
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) and not (precision != precision or recall != recall) and (precision + recall) > 0
          else float("nan"))
    case_accuracy = exact_match_count / len(cases)

    out("")
    out("=" * 78)
    out("整體統計")
    out("=" * 78)
    out(f"案例總數: {len(cases)}")
    out(f"案例級完全正確率 (無誤判也無漏判的案例比例): {exact_match_count}/{len(cases)} = {case_accuracy:.2%}")
    out(f"項目級統計 -> 命中(TP): {total_tp}  誤判(FP): {total_fp}  漏判(FN): {total_fn}")
    out(f"Precision (精確率，抓到的裡面有多少是對的): {precision:.2%}" if precision == precision else "Precision: N/A")
    out(f"Recall    (召回率，該抓的裡面抓到了多少): {recall:.2%}" if recall == recall else "Recall: N/A")
    out(f"F1 Score  : {f1:.2%}" if f1 == f1 else "F1 Score: N/A")
    out("")
    out(f"複製(偵測+遮蔽)平均耗時: {sum(copy_times)/len(copy_times):.3f} ms "
        f"(最快 {min(copy_times):.3f} ms / 最慢 {max(copy_times):.3f} ms)")
    out(f"貼上(還原+重新遮蔽)平均耗時: {sum(paste_times)/len(paste_times):.3f} ms "
        f"(最快 {min(paste_times):.3f} ms / 最慢 {max(paste_times):.3f} ms)")
    out("")
    out("備註：本測試只涵蓋正則規則層 (handler_text.RULES / handler_code.RULES)，")
    out("      不含 Presidio NLP 模型判斷的案例；且流程層是以模擬剪貼簿/按鍵驅動")
    out("      main.py 原始函式，並非真的操作 Windows 剪貼簿/鍵盤。")
    out("=" * 78)

    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n結果已寫入：{RESULT_FILE}")


if __name__ == "__main__":
    main_test()
