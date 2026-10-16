"""
handler_csv.py - 表格檔 (csv / tsv / xlsx) 的敏感資訊遮蔽
====================================================
做法：
  1. 欄位名稱規則：表頭 (第一列) 命中 HEADER_RULES 的欄位，整欄直接換成標籤
     (例如「姓名」欄 -> <PERSON_1>)。這比逐格猜準確，也不會漏掉 NER 認不出的名字。
  2. 其他欄位逐格呼叫 main 傳進來的 anonymize_cell (一般文字規則 + Presidio)。
  3. 同樣的值只處理一次 (快取)，大檔案不會慢到無法使用。

標籤產生與對照表由 main.py 統一管理，這裡只透過 callback 呼叫，所以標籤編號、
clipboard_map.txt 跟其他文字/程式碼檔案完全共用，之後還原也是同一份對照表。

xlsx 需要：pip install openpyxl
"""
import csv
import io
import os
import re

TABLE_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xlsm"}

# (表頭 regex, 標籤類型)。想加欄位規則就加一行；類型只能是大寫英文和底線 (要符合 main 的 TAG_RE)
HEADER_RULES = [
    (re.compile(r"姓名|名字|聯絡人|^(first|last|full|user)?[_ ]?name$", re.I), "PERSON"),
    (re.compile(r"e-?mail|電子郵件|信箱", re.I), "EMAIL_ADDRESS"),
    (re.compile(r"phone|mobile|tel|電話|手機", re.I), "PHONE_NUMBER"),
    (re.compile(r"身分證|身份證|統一編號|national[_ ]?id|^id[_ ]?(no|number)$|ssn", re.I), "TW_NATIONAL_ID"),
    (re.compile(r"address|地址", re.I), "ADDRESS"),
    (re.compile(r"credit|card|信用卡|卡號", re.I), "CREDIT_CARD"),
]


def _column_type(header):
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


class _CellMasker:
    def __init__(self, anonymize_cell, tag_value):
        self.anonymize_cell = anonymize_cell
        self.tag_value = tag_value
        self.cache = {}
        self.changed = 0

    def __call__(self, value, col_type):
        if not isinstance(value, str) or not value.strip() or value.startswith("="):
            return value  # 空白、非字串、Excel 公式都不動
        key = (col_type, value)
        if key not in self.cache:
            if col_type:
                new = self.tag_value(col_type, value.strip())
            else:
                new = self.anonymize_cell(value) or value
            self.cache[key] = new
        new = self.cache[key]
        if new != value:
            self.changed += 1
        return new


def _mask_delimited(path, ext, masker):
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
    col_types = [_column_type(h) for h in rows[0]]
    print(f"[表格] 分隔符號={delimiter!r}，{len(rows)} 列 x {len(rows[0])} 欄，欄位規則命中："
          f"{[(rows[0][i], t) for i, t in enumerate(col_types) if t] or '無'}")

    out = [rows[0]]  # 表頭不處理
    for row in rows[1:]:
        out.append([masker(v, col_types[i] if i < len(col_types) else None) for i, v in enumerate(row)])

    out_path = f"{os.path.splitext(path)[0]}_masked{ext}"
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig 讓 Excel 開中文不亂碼
        csv.writer(f, delimiter=delimiter).writerows(out)
    return out_path


def _mask_xlsx(path, ext, masker):
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("處理 xlsx 需要先安裝 openpyxl：pip install openpyxl")

    wb = openpyxl.load_workbook(path)
    for ws in wb.worksheets:
        header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1), [])]
        col_types = [_column_type(h) for h in header]
        print(f"[表格] 工作表 {ws.title!r}：{ws.max_row} 列 x {ws.max_column} 欄，欄位規則命中："
              f"{[(header[i], t) for i, t in enumerate(col_types) if t] or '無'}")
        for row in ws.iter_rows(min_row=2):
            for i, cell in enumerate(row):
                if isinstance(cell.value, str):
                    cell.value = masker(cell.value, col_types[i] if i < len(col_types) else None)

    out_path = f"{os.path.splitext(path)[0]}_masked{ext}"
    wb.save(out_path)
    return out_path


def mask_file(path, anonymize_cell, tag_value):
    """
    anonymize_cell(text) -> 遮蔽後文字 或 None(沒偵測到)   由 main 提供
    tag_value(etype, val) -> 標籤字串，並自動寫入共用對照表  由 main 提供
    回傳產生的檔案路徑；沒有任何內容被改動則回傳 None (並刪除空轉產生的檔案)。
    """
    ext = os.path.splitext(path)[1].lower()
    masker = _CellMasker(anonymize_cell, tag_value)
    out_path = _mask_xlsx(path, ext, masker) if ext in (".xlsx", ".xlsm") else _mask_delimited(path, ext, masker)
    print(f"[表格] 共遮蔽 {masker.changed} 個儲存格 (相異值 {len(masker.cache)} 種)")
    if masker.changed == 0:
        if out_path and os.path.exists(out_path):
            os.remove(out_path)
        return None
    return out_path