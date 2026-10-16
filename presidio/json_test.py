# -*- coding: utf-8 -*-
"""
測試程式_多版本評分_檔案輸入版.py（放在 presidio 資料夾底下；core.py / handler_*.py 在上一層）

用法：python 測試程式_多版本評分_檔案輸入版.py [檔名]   （省略檔名則用 t1.json）
    .json         : 含 "article" 與 "answer"，走文字/程式碼流程
                    （core.anonymize_text 遮蔽 -> core.deanonymize_text 還原 + 三種評分）
    .pdf / 圖片   : 呼叫 handler_media.mask_pdf / mask_image，答案取自同名 .json 的 "answer"，
                    以「值是否重疊」取代位置比對（只驗證偵測）

說明：
    這版直接呼叫 core.py，不再 import main.py，也不再假造 win32 / pyperclip / pynput 模組。
    快速鍵、模擬貼上、剪貼簿同步屬於 main.py 的流程，不在這支測試範圍內。
    遮蔽時用 save=False，不會把對照表寫到硬碟上。

評分：
    版本一  起訖位置完全一致 + 類型語意相同
    版本二  範圍有重疊 + 類型語意相同（一對多、多對一視為同一個答案，不重複算誤判/漏判）
    版本三  範圍有重疊即可，不看類型
    「類型語意相同」見 LABEL_ALIASES（例如 PERSON = PERSON_NAME、PHONE_NUMBER = TW_PHONE）
"""
import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
import sys, json, time, tempfile, traceback, contextlib

try:  # 先載入 torch，避免之後被 spaCy/thinc 的同名 DLL 搶先造成 [WinError 1114]
    import torch  # noqa: F401
except Exception:
    pass
try:  # 評分表格複製到系統剪貼簿用
    import pyperclip as _SYSTEM_PYPERCLIP
except Exception:
    _SYSTEM_PYPERCLIP = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))   # core.py / handler_*.py 所在資料夾
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif", ".webp"}
PDF_EXTS = {".pdf"}

# 語意相同的類型視為同一類（正規化成左邊的名稱）
LABEL_ALIASES = {
    "PERSON_NAME": "PERSON",
    "PHONE_NUMBER": "PHONE", "TW_PHONE": "PHONE",
    "DATE_OF_BIRTH": "DATE", "DATE_TIME": "DATE",
    "BANK_NAME": "ORGANIZATION",
    "LOCATION": "ADDRESS",
}
VERSIONS = [  # (標題, 位置需完全一致, 類型需相同)
    ("版本一：嚴格比對（起訖位置完全一致 + 類型語意相同）", True, True),
    ("版本二：寬鬆比對（範圍有重疊即可，類型語意需相同）", False, True),
    ("版本三：最寬鬆比對（範圍有重疊即算對，不論類型）", False, False),
]


@contextlib.contextmanager
def _silence_stdout():
    with open(os.devnull, "w", encoding="utf-8") as devnull, contextlib.redirect_stdout(devnull):
        yield


def _fail(step):
    print(f"\n[錯誤] 「{step}」發生例外，程式中止：")
    traceback.print_exc()
    sys.exit(1)


def _resolve_path(name):
    name = name.strip().strip("\"'")
    for p in (name, os.path.join(SCRIPT_DIR, name)):
        if os.path.exists(p):
            return os.path.abspath(p)
    return None


def _pct(x):
    return f"{x:.2%}" if x == x else "N/A"


def _prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    f = 2 * p * r / (p + r) if p == p and r == r and p + r > 0 else float("nan")
    return p, r, f


# ---------------------------- 評分 ----------------------------
# 項目格式統一為 (start, end, value, label)；圖片/PDF 沒有位置，start/end 為 None
def _norm(label):
    return LABEL_ALIASES.get(label, label)


def _hit(e, d, exact, by_label):
    (es, ee, ev, el), (ds, de, dv, dl) = e, d
    if by_label and _norm(el) != _norm(dl):
        return False
    if es is None and ds is None:            # 圖片/PDF：比對值
        ev, dv = ev.strip(), dv.strip()
        return ev == dv if exact else bool(ev and dv and (ev in dv or dv in ev))
    if es is None or ds is None:             # 答案在文章中找不到
        return False
    return (es, ee) == (ds, de) if exact else es < de and ds < ee


def score(expected, detected, exact, by_label):
    """回傳 (TP 數, 誤判清單, 漏判清單)。寬鬆版本中，與答案重疊的多餘偵測/被其他偵測涵蓋的答案不算誤判/漏判。"""
    ok = lambda e, d: _hit(e, d, exact, by_label)
    exp_left, det_left, tp = list(expected), list(detected), 0
    for e in list(exp_left):
        d = next((d for d in det_left if ok(e, d)), None)
        if d:
            exp_left.remove(e); det_left.remove(d); tp += 1
    if not exact:
        det_left = [d for d in det_left if not any(ok(e, d) for e in expected)]
        exp_left = [e for e in exp_left if not any(ok(e, d) for d in detected)]
    return tp, det_left, exp_left


def evaluate(expected, detected, out):
    """寫入三個版本的評分到 out，回傳 [(說明, P, R, F1)] 供複製到剪貼簿。"""
    rows = []
    for title, exact, by_label in VERSIONS:
        tp, fp, fn = score(expected, detected, exact, by_label)
        p, r, f = _prf(tp, len(fp), len(fn))
        rows.append((title.split("（", 1)[-1].rstrip("）"), p, r, f))
        out(f"\n{'-' * 78}\n{title}\n{'-' * 78}")
        out(f"命中(TP): {tp}  誤判(FP): {len(fp)}  漏判(FN): {len(fn)}")
        if fp:
            out(f"誤判內容: {[(v, l) for _, _, v, l in fp]}")
        if fn:
            out(f"漏判內容: {[(v, l) for _, _, v, l in fn]}")
        out(f"Precision: {_pct(p)}\nRecall   : {_pct(r)}\nF1 Score : {_pct(f)}")
        out(f"結果: {'✔ 完全正確' if not fp and not fn else '✘ 有誤判或漏判'}")
    return rows


def finish(lines, rows, result_file):
    """寫報告檔，並把 Tab 分隔的結果表格複製到系統剪貼簿（可直接貼 Excel）。"""
    lines.append("=" * 78)
    try:
        with open(result_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        _fail(f"寫入報告檔 {result_file}")
    print(f"報告已寫入：{result_file}")
    n = lambda x: f"{x:.4f}" if x == x else "N/A"
    table = "\n".join(["\t".join(["比對機制與說明", "Precision", "Recall", "F1 Score"])] +
                      ["\t".join([d, n(p), n(r), n(f)]) for d, p, r, f in rows])
    try:
        _SYSTEM_PYPERCLIP.copy(table)
        print("[剪貼簿] 已複製本次評分表格，可直接貼到 Excel")
    except Exception as e:
        print(f"[警告] 複製表格到剪貼簿失敗（報告檔已寫入）：{e}")


# ------------------------ 分支一：json（文字/程式碼）------------------------
def run_text_flow(article, answer, result_file):
    try:
        import core
    except Exception:
        _fail("匯入 core.py（請確認 core.py / handler_*.py 在 presidio 的上一層資料夾）")

    def detect(text):
        """與 core.anonymize_text 相同的偵測 + 重疊處理，但回傳位置供評分用。"""
        try:
            with _silence_stdout():
                is_code = core.is_code_text(text)
                h = core.handler_code if is_code else core.handler_text
                matches = list(h.detect(text))
                if core.handler_field is not None:
                    matches += list(core.handler_field.detect(text))
                matches = core.merge_overlaps(matches, core.handler_text.contains_chinese(text))
        except Exception:
            _fail("文字/程式碼偵測")
        return is_code, [(s, e, text[s:e], label) for s, e, label, *_ in matches]

    def timed(step, fn):
        t = time.perf_counter()
        try:
            with _silence_stdout():
                result = fn()
        except Exception:
            _fail(step)
        return (time.perf_counter() - t) * 1000, result

    print("\n[暖機] 載入模型中（不計入計時）...")
    try:
        with _silence_stdout():
            core.warmup()
    except Exception:
        _fail("core.warmup")
    detect("暖機用測試文字 test123 test@example.com")
    print("[暖機] 完成\n")

    lines, mapping = [], {}
    out = lines.append
    out("=" * 78)
    out("剪貼簿敏感資訊防護 - 單篇文章 遮蔽/還原 測試報告（多版本評分，透過 core）")
    out(f"文章長度：{len(article)} 字元　答案筆數：{len(answer)}")
    out("=" * 78)

    # 遮蔽 (save=False：不寫對照表檔到硬碟)；沒偵測到任何內容時 anonymize_text 回傳 None
    t_mask, masked = timed("遮蔽(core.anonymize_text)",
                           lambda: core.anonymize_text(article, mapping, is_code=None, save=False))
    masked = masked if masked is not None else article
    t_restore, restored = timed("還原(core.deanonymize_text)",
                                lambda: core.deanonymize_text(masked, mapping))
    restored_ok = restored == article

    out(f"\n[遮蔽] 整篇偵測+遮蔽耗時: {t_mask:.3f} ms | 對照表筆數: {len(mapping)}")
    out(f"[還原] 耗時: {t_restore:.3f} ms | 完全還原成原始文章: {'OK' if restored_ok else 'FAIL'}")
    if not restored_ok:
        out(f"  還原後長度 {len(restored)} vs 原始長度 {len(article)}")
    out("(快速鍵 / 貼上流程屬於 main.py，不在此測試範圍)")

    is_code, detected = detect(article)
    expected, cursor = [], {}
    for value, label in answer:   # 同值多次出現時，依序對應到不同位置
        idx = article.find(value, cursor.get(value, 0))
        if idx == -1:
            idx = article.find(value)
        if idx == -1:
            expected.append((None, None, value, label))
        else:
            cursor[value] = idx + 1
            expected.append((idx, idx + len(value), value, label))

    out(f"[分類] 文章整體判斷: {'程式碼' if is_code else '一般文字'}")
    out(f"\n解答共 {len(answer)} 筆: {answer}")
    out(f"實際偵測到 {len(detected)} 筆: {[(v, l) for _, _, v, l in detected]}")
    finish(lines, evaluate(expected, detected, out), result_file)


# ---------------------------- 分支二：pdf / 圖片 ----------------------------
def _ocr_raw_text(hm, path, ext, zoom=2.0):
    """用 handler_media 自己的 Windows OCR 取得原始辨識文字，僅供報告開頭肉眼比對，不影響評分。"""
    def ocr(img):
        words = hm.ocr_words(hm.preprocess_for_ocr(img)[0])
        return "\n".join(" ".join(w["text"] for w in line) for line in hm._group_lines(words))
    try:
        if ext not in PDF_EXTS:
            return ocr(hm.Image.open(path).convert("RGB")) or "(OCR 結果為空白)"
        doc, pages = hm.fitz.open(path), []
        try:
            for page in doc:
                if hm._page_has_text(page):
                    pages.append(f"--- 第 {page.number + 1} 頁（PDF 內嵌文字層）---\n{page.get_text().strip()}")
                else:
                    pix = page.get_pixmap(matrix=hm.fitz.Matrix(zoom, zoom), annots=False)
                    img = hm.Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    pages.append(f"--- 第 {page.number + 1} 頁（Windows OCR）---\n{ocr(img) or '(本頁 OCR 結果為空白)'}")
        finally:
            doc.close()
        return "\n\n".join(pages) or "(空白/無法辨識)"
    except Exception:
        return "(OCR 原始文字擷取失敗；不影響下方評分)\n" + traceback.format_exc()


def run_media_flow(path, answer, result_file):
    try:
        import handler_media as hm
    except Exception:
        _fail("匯入 handler_media.py（請確認與 core.py 同資料夾且相關套件已安裝）")

    ext = os.path.splitext(path)[1].lower()
    func = hm.mask_pdf if ext in PDF_EXTS else hm.mask_image
    tmp_dir = tempfile.mkdtemp(prefix="presidio_media_test_")
    output = os.path.join(tmp_dir, f"{os.path.splitext(os.path.basename(path))[0]}_masked{ext}")

    print("[暖機] 載入模型中（不計入計時）...")
    try:
        hm.detect_matches("暖機用測試文字 test123 test@example.com")
    except Exception:
        traceback.print_exc()
    print("[OCR] 擷取原始辨識文字...")
    raw_text = _ocr_raw_text(hm, path, ext)

    t = time.perf_counter()
    try:
        entries = func(path, output, os.path.join(tmp_dir, "media_test_map.json"))
    except Exception:
        _fail(f"handler_media.{func.__name__} 偵測+遮蔽")
    elapsed = (time.perf_counter() - t) * 1000

    detected = [(None, None, (e.get("value") or "").strip(), e["label"]) for e in entries]
    expected = [(None, None, str(v).strip(), l) for v, l in answer]

    lines = []
    out = lines.append
    out("=" * 78)
    out("[OCR 原始辨識文字]（僅供肉眼比對，與下方評分獨立）")
    out("-" * 78)
    out(raw_text)
    out("=" * 78)
    out("\n" + "=" * 78)
    out("剪貼簿敏感資訊防護 - 圖片/PDF 偵測測試報告（多版本評分，透過 handler_media）")
    out(f"輸入檔案：{path}\n答案筆數：{len(expected)}")
    out("=" * 78)
    out(f"\n[偵測+遮蔽] handler_media.{func.__name__} 耗時: {elapsed:.3f} ms")
    out(f"\n解答共 {len(expected)} 筆: {[(v, l) for _, _, v, l in expected]}")
    out(f"實際偵測到 {len(detected)} 筆: {[(v, l) for _, _, v, l in detected]}")
    rows = evaluate(expected, detected, out)
    out(f"\n(遮蔽後檔案存在暫存資料夾: {tmp_dir})")
    finish(lines, rows, result_file)


# ------------------------------- 進入點 -------------------------------
def main_test():
    raw = sys.argv[1] if len(sys.argv) > 1 else "t1.json"
    path = _resolve_path(raw)
    if path is None:
        sys.exit(f"[錯誤] 找不到檔案：{raw}")
    stem, ext = os.path.splitext(os.path.basename(path))
    ext = ext.lower()
    result_file = os.path.join(SCRIPT_DIR, f"{stem}_{ext.lstrip('.')}_result.txt")

    if ext == ".json":
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            article, answer = data["article"], data["answer"]
        except Exception:
            _fail(f"讀取測試檔 {path}（需為合法 JSON，含 article / answer）")
        run_text_flow(article, answer, result_file)
    elif ext in PDF_EXTS | IMAGE_EXTS:
        ans_path = _resolve_path(os.path.splitext(raw)[0] + ".json")
        if ans_path is None:
            sys.exit(f"[錯誤] 找不到答案檔：{os.path.splitext(raw)[0]}.json")
        try:
            with open(ans_path, encoding="utf-8") as f:
                answer = json.load(f)["answer"]
        except Exception:
            _fail(f"讀取答案檔 {ans_path}（需為合法 JSON，含 answer）")
        run_media_flow(path, answer, result_file)
    else:
        sys.exit(f"[錯誤] 不支援的檔案類型：{ext}")


if __name__ == "__main__":
    main_test()