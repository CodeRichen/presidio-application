"""
handler_csv.py - 表格檔 (csv / tsv / xlsx) 的敏感資訊遮蔽 (整欄遮蔽版)
====================================================
規則：只要一個欄位「有任何一格」含個資，整個欄位 (表頭以外的所有儲存格) 都換成標籤。
判斷順序：
  1. 表頭命中 HEADER_RULES -> 整欄遮蔽，類型由規則決定 (例如「姓名」-> PERSON)
  2. 否則逐格偵測 (detect_cell)，只要有任何一格偵測到個資 -> 整欄遮蔽，
     類型取該欄位偵測到最多的類型
  3. 都沒有 -> 整欄不動
同樣的值永遠對應同一個標籤 (同欄、跨欄、跨檔案都一致)，之後可用同一份對照表還原。

介面 (由 core.py 提供 callback，標籤編號與對照表跟其他檔案完全共用)：
    detect_cell(text) -> 帶標籤的遮蔽後文字，或 None (沒偵測到)。
        只用來判斷「這格有沒有個資 / 是什麼類型」，不應寫入共用對照表
        (core 傳進來的版本用的是暫時的空對照表)。
    tag_value(etype, val) -> 標籤字串，並自動寫入共用對照表

xlsx 需要：pip install openpyxl
"""
import csv
import io
import os
import re
from collections import Counter

TABLE_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xlsm"}

# 偵測結果中，這些類型不拿來決定「整欄要不要遮蔽」(例如不想因為日期/網址就整欄遮蔽)。
# 例：{"DATE_TIME", "URL"}。預設不排除。
IGNORE_TYPES_FOR_COLUMN = set()

# (表頭 regex, 標籤類型)。想加欄位規則就加一行；類型只能是大寫英文和底線 (要符合 TAG_RE)
HEADER_RULES = [
    (re.compile(r"姓名|名字|聯絡人|^(first|last|full|user)?[_ ]?name$", re.I), "PERSON"),
    (re.compile(r"e-?mail|電子郵件|信箱", re.I), "EMAIL_ADDRESS"),
    (re.compile(r"phone|mobile|tel|電話|手機", re.I), "PHONE_NUMBER"),
    (re.compile(r"身分證|身份證|統一編號|national[_ ]?id|^id[_ ]?(no|number)$|ssn", re.I), "TW_NATIONAL_ID"),
    (re.compile(r"address|地址", re.I), "ADDRESS"),
    (re.compile(r"credit|card|信用卡|卡號", re.I), "CREDIT_CARD"),
]

_TAG_TYPE_RE = re.compile(r"<([A-Z_]+)_\d+>")


def _header_type(header):
    h = str(header or "").strip()
    for pattern, etype in HEADER_RULES:
        if pattern.search(h):
            return etype
    return None


def _decode(raw: bytes) -> str:
    # 台灣常見的 CSV 可能是 UTF-8 (含 BOM) 或 Excel 另存的 Big5/CP950
    for enc in ("utf-8-sig", "cp950"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _is_candidate(value):
    """可以拿去做偵測的儲存格：非空字串、不是 Excel 公式"""
    return isinstance(value, str) and bool(value.strip()) and not value.startswith("=")


def _decide_column_types(header, values_by_col, detect_cell):
    """回傳 (col_types, report)。col_types[i] 是第 i 欄的標籤類型，None 表示不遮蔽。
    values_by_col[i] 是第 i 欄表頭以外的所有儲存格值。"""
    cache = {}  # 同一個值只偵測一次
    col_types, report = [], []
    for i, values in enumerate(values_by_col):
        name = header[i] if i < len(header) else None

        etype = _header_type(name)
        if etype:
            col_types.append(etype)
            report.append((name, etype, "表頭規則"))
            continue

        counter = Counter()
        hit_cells = 0
        for v in values:
            if not _is_candidate(v):
                continue
            if v not in cache:
                masked = detect_cell(v)
                cache[v] = [t for t in _TAG_TYPE_RE.findall(masked)
                            if t not in IGNORE_TYPES_FOR_COLUMN] if masked else []
            if cache[v]:
                hit_cells += 1
                counter.update(cache[v])

        if counter:
            etype = counter.most_common(1)[0][0]
            col_types.append(etype)
            report.append((name, etype, f"內容偵測到個資 ({hit_cells} 格)"))
        else:
            col_types.append(None)
    return col_types, report


class _CellMasker:
    def __init__(self, tag_value):
        self.tag_value = tag_value
        self.cache = {}
        self.changed = 0

    def __call__(self, value, col_type):
        """整欄遮蔽：欄位被判定有個資時，除了空白與公式外的每一格都換成標籤 (數字/日期也會轉成文字標籤)"""
        if col_type is None or value is None:
            return value
        if isinstance(value, str) and (not value.strip() or value.startswith("=")):
            return value
        text = str(value).strip()
        if not text:
            return value
        key = (col_type, text)
        if key not in self.cache:
            self.cache[key] = self.tag_value(col_type, text)
        self.changed += 1
        return self.cache[key]


def _print_report(label, nrows, ncols, report):
    flagged = [f"{name!r}->{etype} ({why})" for name, etype, why in report]
    print(f"[表格] {label}：{nrows} 列 x {ncols} 欄，整欄遮蔽的欄位：{flagged or '無'}")


def _mask_delimited(path, ext, detect_cell, masker):
    with open(path, "rb") as f:
        text = _decode(f.read())

    if ext == ".tsv":
        delimiter = "\t"
    else:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","

    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    if not rows:
        return None

    header = rows[0]
    ncols = max(len(r) for r in rows)
    values_by_col = [[r[i] if i < len(r) else None for r in rows[1:]] for i in range(ncols)]
    col_types, report = _decide_column_types(header, values_by_col, detect_cell)
    _print_report(f"分隔符號={delimiter!r}", len(rows), ncols, report)

    out = [header]  # 表頭不處理
    for row in rows[1:]:
        out.append([masker(v, col_types[i]) for i, v in enumerate(row)])

    out_path = f"{os.path.splitext(path)[0]}_masked{ext}"
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig 讓 Excel 開中文不亂碼
        csv.writer(f, delimiter=delimiter).writerows(out)
    return out_path


def _mask_xlsx(path, ext, detect_cell, masker):
    try:
        import openpyxl
        from openpyxl.cell.cell import MergedCell
    except ImportError:
        raise RuntimeError("處理 xlsx 需要先安裝 openpyxl：pip install openpyxl")

    wb = openpyxl.load_workbook(path)
    for ws in wb.worksheets:
        rows = list(ws.iter_rows())
        if not rows:
            continue
        header = [c.value for c in rows[0]]
        ncols = max(len(r) for r in rows)
        values_by_col = [[r[i].value if i < len(r) else None for r in rows[1:]] for i in range(ncols)]
        col_types, report = _decide_column_types(header, values_by_col, detect_cell)
        _print_report(f"工作表 {ws.title!r}", len(rows), ncols, report)

        for row in rows[1:]:
            for i, cell in enumerate(row):
                if isinstance(cell, MergedCell):
                    continue  # 合併儲存格的非左上角格子是唯讀的
                cell.value = masker(cell.value, col_types[i])

    out_path = f"{os.path.splitext(path)[0]}_masked{ext}"
    wb.save(out_path)
    return out_path


def mask_file(path, detect_cell, tag_value):
    """回傳產生的檔案路徑；沒有任何欄位被遮蔽則回傳 None (並刪除空轉產生的檔案)。"""
    ext = os.path.splitext(path)[1].lower()
    masker = _CellMasker(tag_value)
    if ext in (".xlsx", ".xlsm"):
        out_path = _mask_xlsx(path, ext, detect_cell, masker)
    else:
        out_path = _mask_delimited(path, ext, detect_cell, masker)
    print(f"[表格] 共遮蔽 {masker.changed} 個儲存格 (相異值 {len(masker.cache)} 種)")
    if masker.changed == 0:
        if out_path and os.path.exists(out_path):
            os.remove(out_path)
        return None
    return out_path