# -*- coding: utf-8 -*-
"""
測試程式_多版本評分_檔案輸入版.py（放在 presidio 資料夾底下；core.py / handler_*.py 在上一層）

用法：python 測試程式_多版本評分_檔案輸入版.py [檔名] [--ablation] [--no-face]   （省略檔名則用 man.jpg）
    --ablation    只對 .json：同一篇文章依序用「不前處理 / 每個前處理步驟單獨開 / 目前預設 / 舊版全開」各跑一次，
                  列出 P / R / F1，並指出每個設定相對於「不前處理」多命中、漏掉、多誤判了哪些實體，
                  用來找出是哪一個前處理步驟讓分數變差 (或變好)。
    --no-face     只對 .pdf / 圖片：關閉人臉遮蔽 (預設開啟)，同時略過人臉測速。
    .json         : 含 "article" 與 "answer"，走文字/程式碼流程
                    （core.anonymize_text 遮蔽 -> core.deanonymize_text 還原 + 三種評分）
    .pdf / 圖片   : 呼叫 handler_media.mask_pdf / mask_image，答案取自同名 .json 的 "answer"，
                    以「值是否重疊」取代位置比對（只驗證偵測）

人臉測速 (.pdf / 圖片，人臉遮蔽開啟時)：
    1. 人臉偵測本身：冷啟動 (重新載入 YuNet + 首次推論) 與每頁/每張圖的平均、最快耗時 (重複 FACE_BENCH_REPEAT 次)
    2. 遮蔽總耗時：「人臉遮蔽開啟」與「關閉」各跑一次完整遮蔽，兩者差即人臉造成的額外耗時
       (OCR 本身有些微波動，差值僅供參考；想要更準可多跑幾次取平均)
    3. 人臉不列入文字評分 (label=FACE 的偵測項目不算誤判)；若答案 json 的 answer 內有 label 為 "FACE" 的項目，
       只用來比對人臉數量 (例如 ["[人臉]", "FACE"] 寫幾筆代表有幾張臉)

說明：
    這版直接呼叫 core.py，不再 import main.py，也不再假造 win32 / pyperclip / pynput 模組。
    偵測走 core.detect_entities，也就是和實際遮蔽相同的流程 (含 preprocess 前處理、已知實體 / 敏感清單 / 安全清單)；
    評分時預設【不套用】敏感清單 / known_entities.txt / 安全清單 (見下方 USE_LISTS)：這些清單是使用過程中累積的，
    混進來會讓不同次的分數不能比較，清單裡若有答案也等於拿答案考試。
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
USE_LISTS = False   # True 才會把敏感清單 / known_entities.txt / 安全清單算進偵測 (評分時請維持 False)
FACE_BENCH_REPEAT = 5   # 人臉偵測測速：每張圖重複次數
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


def _apply_list_setting(core):
    """USE_LISTS=False 時把三種清單都關掉 (只影響這個測試程序，不會改到任何清單檔)"""
    core.USE_KNOWN_ITEMS = USE_LISTS
    if not USE_LISTS:
        core.entity_store.safe_values = lambda: frozenset()
        core.handler_text.detect_known_entities = lambda text: []   # handler_text.detect 內部也是呼叫這個名稱


def _expected_positions(article, answer):
    """答案 (值, 類型) -> (start, end, 值, 類型)；同值多次出現時，依序對應到不同位置"""
    expected, cursor = [], {}
    for value, label in answer:
        idx = article.find(value, cursor.get(value, 0))
        if idx == -1:
            idx = article.find(value)
        if idx == -1:
            expected.append((None, None, value, label))
        else:
            cursor[value] = idx + 1
            expected.append((idx, idx + len(value), value, label))
    return expected


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
    _apply_list_setting(core)

    def detect(text):
        """與 core.anonymize_text 完全相同的偵測 (前處理 -> 規則與模型 -> 座標換算回原文 -> 清單 -> 重疊處理)，
        但回傳位置供評分用。座標是原文的位置，所以和答案的位置可以直接比對。"""
        try:
            with _silence_stdout():
                is_code, matches = core.detect_entities(text)
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
    out(f"清單 (敏感清單 / known_entities / 安全清單)：{'開啟' if USE_LISTS else '關閉 (評分用)'}")
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
    expected = _expected_positions(article, answer)

    out(f"[分類] 文章整體判斷: {'程式碼' if is_code else '一般文字'}")
    out(f"\n解答共 {len(answer)} 筆: {answer}")
    out(f"實際偵測到 {len(detected)} 筆: {[(v, l) for _, _, v, l in detected]}")
    finish(lines, evaluate(expected, detected, out), result_file)


# ---------------------------- 前處理逐項比較 (--ablation) ----------------------------
_PP_KEYS = ["ENABLED", "OPT_NFKC", "NFKC_KEEP", "OPT_INVISIBLE", "OPT_HTML", "CHINESE_CONVERT", "OPT_WHITESPACE"]


def run_ablation(article, answer, result_file):
    try:
        import core
        import preprocess as pp
    except Exception:
        _fail("匯入 core.py / preprocess.py")
    _apply_list_setting(core)

    default = {k: getattr(pp, k) for k in _PP_KEYS}
    off = dict(default, ENABLED=True, OPT_NFKC=False, OPT_INVISIBLE=False, OPT_HTML=False,
               CHINESE_CONVERT=None, OPT_WHITESPACE=False)
    configs = [
        ("0 不做前處理 (基準)", dict(default, ENABLED=False)),
        ("1 只做 NFKC (保留中文標點)", dict(off, OPT_NFKC=True)),
        ("2 只做 NFKC (中文標點也轉半形)", dict(off, OPT_NFKC=True, NFKC_KEEP="")),
        ("3 只做隱形字元清理", dict(off, OPT_INVISIBLE=True)),
        ("4 只做網頁標籤處理", dict(off, OPT_HTML=True)),
        ("5 只做簡繁統一 auto", dict(off, CHINESE_CONVERT="auto")),
        ("6 只做簡轉繁 (一律)", dict(off, CHINESE_CONVERT="s2t")),
        ("7 只做繁轉簡 (一律)", dict(off, CHINESE_CONVERT="t2s")),
        ("8 只做空白換行整理", dict(off, OPT_WHITESPACE=True)),
        ("9 目前預設組合", dict(default)),
        ("10 舊版全開 (NFKC 全轉 / 空白整理 / 一律簡轉繁)",
         dict(default, ENABLED=True, OPT_NFKC=True, NFKC_KEEP="", OPT_INVISIBLE=True, OPT_HTML=True,
              CHINESE_CONVERT="s2t", OPT_WHITESPACE=True)),
    ]
    expected = _expected_positions(article, answer)

    print("\n[暖機] 載入模型中...")
    try:
        with _silence_stdout():
            core.warmup()
    except Exception:
        _fail("core.warmup")
    print("[暖機] 完成\n")

    results = []
    for name, cfg in configs:
        for k, v in cfg.items():
            setattr(pp, k, v)
        try:
            with _silence_stdout():
                _, matches = core.detect_entities(article)
        except Exception:
            _fail(f"偵測 ({name})")
        detected = [(s_, e_, article[s_:e_], label) for s_, e_, label, *_ in matches]
        row = {"name": name, "n_det": len(detected)}
        for key, (_, exact, by_label) in zip(("v1", "v2", "v3"), VERSIONS):
            tp, fp, fn = score(expected, detected, exact, by_label)
            row[key] = (tp, fp, fn, *_prf(tp, len(fp), len(fn)))
        results.append(row)
    for k, v in default.items():          # 還原設定
        setattr(pp, k, v)

    lines = []
    out = lines.append
    out("=" * 100)
    out(f"前處理逐項比較 (--ablation)　文章 {len(article)} 字元　答案 {len(answer)} 筆　清單: {'開啟' if USE_LISTS else '關閉'}")
    out("版本一=位置完全一致+類型相同　版本二=範圍重疊+類型相同　版本三=範圍重疊(不看類型)")
    out("=" * 100)
    out(f"{'設定':<42}{'偵測':>5} | {'v1 TP/FP/FN':>12}{'P':>8}{'R':>8}{'F1':>8}{'ΔF1':>8} | {'v2 F1':>8}{'v3 F1':>8}")
    base_f1 = results[0]["v1"][5]
    pct = lambda x: f"{x:.1%}" if x == x else "N/A"
    for r in results:
        tp, fp, fn, p, rc, f1 = r["v1"]
        delta = f"{(f1 - base_f1) * 100:+.1f}" if f1 == f1 and base_f1 == base_f1 else "N/A"
        out(f"{r['name']:<42}{r['n_det']:>5} | {f'{tp}/{len(fp)}/{len(fn)}':>12}{pct(p):>8}{pct(rc):>8}{pct(f1):>8}{delta:>8} | "
            f"{pct(r['v2'][5]):>8}{pct(r['v3'][5]):>8}")

    out("\n" + "=" * 100)
    out("相對於「不做前處理」(版本一，位置完全一致) 的差異：")
    base_fp, base_fn = set(results[0]["v1"][1]), set(results[0]["v1"][2])
    fmt = lambda it: f"{pp.vis(it[2], 36)!r} [{it[3]}] @{it[0]}:{it[1]}"
    for r in results[1:]:
        fp, fn = set(r["v1"][1]), set(r["v1"][2])
        found, lost = sorted(base_fn - fn, key=str), sorted(fn - base_fn, key=str)
        new_fp, gone_fp = sorted(fp - base_fp, key=str), sorted(base_fp - fp, key=str)
        if not (found or lost or new_fp or gone_fp):
            out(f"\n▸ {r['name']}：和基準完全相同")
            continue
        out(f"\n▸ {r['name']}")
        for it in found:
            out(f"    + 多命中 (原本漏掉): {fmt(it)}")
        for it in lost:
            out(f"    - 不再命中 (原本抓到): {fmt(it)}")
        for it in new_fp:
            out(f"    ! 新增誤判: {fmt(it)}")
        for it in gone_fp:
            out(f"    ✓ 少了一筆誤判: {fmt(it)}")
    out("=" * 100)
    print("\n".join(lines))
    try:
        with open(result_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"報告已寫入：{result_file}")
    except Exception:
        _fail(f"寫入報告檔 {result_file}")


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


# ---------------------------- 人臉測速 ----------------------------
def _load_face_images(hm, path, ext, zoom=2.0):
    """回傳 [(標籤, PIL 影像)]。圖片取套用 EXIF 方向後的原圖；PDF 以 zoom=2 逐頁渲染 (與 handler_media 實際遮蔽相同)。"""
    items = []
    if ext in PDF_EXTS:
        doc = hm.fitz.open(path)
        try:
            for page in doc:
                pix = page.get_pixmap(matrix=hm.fitz.Matrix(zoom, zoom), colorspace=hm.fitz.csRGB,
                                      alpha=False, annots=False)
                items.append((f"第 {page.number + 1} 頁", hm.Image.frombytes("RGB", (pix.width, pix.height), pix.samples)))
        finally:
            doc.close()
    else:
        with hm.Image.open(path) as src:
            items.append(("圖片", hm.ImageOps.exif_transpose(src).convert("RGB")))
    return items


def _face_bench(hm, path, ext, repeat=FACE_BENCH_REPEAT):
    """量測人臉偵測本身的耗時。回傳 (冷啟動 ms, [(標籤, 尺寸, 偵測到幾張臉, 平均 ms, 最快 ms)])。
    冷啟動 = 清掉已載入的 YuNet 後，第一次呼叫 detect_faces (含載入模型 + 首次推論)。"""
    images = _load_face_images(hm, path, ext)
    if not images:
        return 0.0, []
    hm._yunet = None   # 強制重新載入模型，才量得到冷啟動
    t = time.perf_counter()
    hm.detect_faces(images[0][1])
    cold = (time.perf_counter() - t) * 1000
    rows = []
    for label, img in images:
        times, n = [], 0
        for _ in range(repeat):
            t = time.perf_counter()
            faces = hm.detect_faces(img)
            times.append((time.perf_counter() - t) * 1000)
            n = len(faces)
        rows.append((label, img.size, n, sum(times) / len(times), min(times)))
    return cold, rows


def run_media_flow(path, answer, result_file):
    try:
        import handler_media as hm
    except Exception:
        _fail("匯入 handler_media.py（請確認與 core.py 同資料夾且相關套件已安裝）")

    ext = os.path.splitext(path)[1].lower()
    func = hm.mask_pdf if ext in PDF_EXTS else hm.mask_image
    tmp_dir = tempfile.mkdtemp(prefix="presidio_media_test_")
    stem = os.path.splitext(os.path.basename(path))[0]
    output = os.path.join(tmp_dir, f"{stem}_masked{ext}")
    face_on = hm.face_enabled()

    print("[暖機] 載入模型中（不計入計時）...")
    try:
        hm.detect_matches("暖機用測試文字 test123 test@example.com")
    except Exception:
        traceback.print_exc()
    print("[OCR] 擷取原始辨識文字...")
    raw_text = _ocr_raw_text(hm, path, ext)

    bench = None
    if face_on:
        print(f"[人臉] 量測人臉偵測耗時（每張重複 {FACE_BENCH_REPEAT} 次）...")
        try:
            bench = _face_bench(hm, path, ext)
        except Exception:
            _fail("人臉偵測測速（缺模型 / opencv 時可加 --no-face 略過）")

    t = time.perf_counter()
    try:
        entries = func(path, output, os.path.join(tmp_dir, "media_test_map.json"))
    except Exception:
        _fail(f"handler_media.{func.__name__} 偵測+遮蔽")
    elapsed = (time.perf_counter() - t) * 1000

    # 另跑一次「關閉人臉遮蔽」的完整遮蔽，兩者差即人臉造成的額外耗時
    elapsed_nf = None
    if face_on:
        print("[人臉] 另跑一次關閉人臉的遮蔽，用來比較耗時...")
        prev = os.environ.get("PII_FACE_ENABLED")
        os.environ["PII_FACE_ENABLED"] = "0"
        try:
            t = time.perf_counter()
            try:
                with _silence_stdout():
                    func(path, os.path.join(tmp_dir, f"{stem}_nofaces{ext}"),
                         os.path.join(tmp_dir, "media_test_map_nofaces.json"))
            except Exception:
                _fail(f"handler_media.{func.__name__}（關閉人臉）")
            elapsed_nf = (time.perf_counter() - t) * 1000
        finally:
            if prev is None:
                os.environ.pop("PII_FACE_ENABLED", None)
            else:
                os.environ["PII_FACE_ENABLED"] = prev

    # 人臉不列入文字評分；答案裡 label=FACE 的項目只用來比對人臉數量
    face_entries = [e for e in entries if e["label"] == "FACE"]
    text_entries = [e for e in entries if e["label"] != "FACE"]
    answer_text = [(v, l) for v, l in answer if l != "FACE"]
    expected_faces = len(answer) - len(answer_text)

    detected = [(None, None, (e.get("value") or "").strip(), e["label"]) for e in text_entries]
    expected = [(None, None, str(v).strip(), l) for v, l in answer_text]

    lines = []
    out = lines.append
    out("=" * 78)
    out("[OCR 原始辨識文字]（僅供肉眼比對，與下方評分獨立）")
    out("-" * 78)
    out(raw_text)
    out("=" * 78)
    out("\n" + "=" * 78)
    out("剪貼簿敏感資訊防護 - 圖片/PDF 偵測測試報告（多版本評分，透過 handler_media）")
    out(f"輸入檔案：{path}\n答案筆數：{len(expected)}　人臉遮蔽：{'開啟' if face_on else '關閉'}")
    out("=" * 78)
    out(f"\n[偵測+遮蔽] handler_media.{func.__name__} 耗時: {elapsed:.3f} ms"
        f"（{'含' if face_on else '不含'}人臉遮蔽）")

    if face_on:
        cold, rows = bench
        out("\n" + "-" * 78)
        out("[人臉偵測測速] 本機 YuNet（僅人臉偵測本身，不含 OCR / 塗黑 / 輸出）")
        out("-" * 78)
        out(f"冷啟動（載入模型 + 第一次推論）: {cold:.1f} ms")
        for label, size, n, avg, best in rows:
            out(f"  {label}  {size[0]}x{size[1]}px  偵測到 {n} 張臉  平均 {avg:.1f} ms / 最快 {best:.1f} ms"
                f"  (重複 {FACE_BENCH_REPEAT} 次)")
        if rows:
            total_avg = sum(r[3] for r in rows)
            out(f"全部合計（熱啟動平均）: {total_avg:.1f} ms　平均每張 {total_avg / len(rows):.1f} ms")
        diff = elapsed - elapsed_nf
        out(f"\n完整遮蔽耗時：人臉開啟 {elapsed:.1f} ms｜人臉關閉 {elapsed_nf:.1f} ms｜"
            f"差 {diff:+.1f} ms（{diff / elapsed_nf:+.1%}）" if elapsed_nf else "")
        out("（兩次完整遮蔽之間 OCR 本身會有波動，差值僅供參考；實際遮蔽時模型已預載，不含冷啟動）")
        out(f"遮蔽的人臉數: {len(face_entries)}"
            + (f"　答案人臉數: {expected_faces}　{'✔ 一致' if len(face_entries) == expected_faces else '✘ 不一致'}"
               if expected_faces else "　(答案未標示人臉數，未比對)"))
        if face_entries:
            scores = [f"{e['score']:.2f}" for e in face_entries if e.get("score") is not None]
            out(f"人臉信心分數: {', '.join(scores)}")

    out(f"\n解答共 {len(expected)} 筆: {[(v, l) for _, _, v, l in expected]}")
    out(f"實際偵測到 {len(detected)} 筆（不含人臉）: {[(v, l) for _, _, v, l in detected]}")
    rows = evaluate(expected, detected, out)
    out(f"\n(遮蔽後檔案存在暫存資料夾: {tmp_dir})")
    finish(lines, rows, result_file)


# ------------------------------- 進入點 -------------------------------
def main_test():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    ablation = "--ablation" in sys.argv[1:]
    if "--no-face" in sys.argv[1:]:
        os.environ["PII_FACE_ENABLED"] = "0"
    raw = args[0] if args else "t1.pdf"
    path = _resolve_path(raw)
    if path is None:
        sys.exit(f"[錯誤] 找不到檔案：{raw}")
    stem, ext = os.path.splitext(os.path.basename(path))
    ext = ext.lower()
    result_file = os.path.join(SCRIPT_DIR, f"{stem}_{ext.lstrip('.')}_{'ablation' if ablation else 'result'}.txt")

    if ext == ".json":
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            article, answer = data["article"], data["answer"]
        except Exception:
            _fail(f"讀取測試檔 {path}（需為合法 JSON，含 article / answer）")
        (run_ablation if ablation else run_text_flow)(article, answer, result_file)
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