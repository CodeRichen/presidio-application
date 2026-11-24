"""handler_csv.py — 離線表格敏感資料「整欄」遮蔽，與團隊 core.py 相容。

規則：表頭命中敏感欄位規則，或任一資料格（含 Excel 公式內的文字常數／
      已快取的公式結果）命中 PII，即遮蔽該欄所有非空資料格（不含表頭）。
支援 .csv/.tsv/.xlsx/.xlsm；表格在原位置輸出 *_masked.<副檔名>。

注意：本程式處理儲存格及其註解/超連結，不是全檔案鑑識級清洗。
      隱藏物件、圖片、圖表、VBA、工作簿屬性、外部連結與壓縮檔中殘留資訊
      仍可能帶有敏感資料。請勿直接宣稱輸出 XLSX/XLSM 已完全去識別化。
"""
from __future__ import annotations

import csv
import io
import os
import re
from collections import Counter
import unicodedata

TABLE_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xlsm"}

# 如不希望因日期、網址等類型就觸發「整欄遮蔽」，可設定此集合。
IGNORE_TYPES_FOR_COLUMN = set()

HEADER_RULES = [
    (re.compile(r"姓名|名字|聯絡人|^(first|last|full|user)?[_ ]?name$", re.I), "PERSON"),
    (re.compile(r"e-?mail|電子郵件|信箱", re.I), "EMAIL_ADDRESS"),
    (re.compile(r"phone|mobile|tel|電話|手機", re.I), "PHONE_NUMBER"),
    (re.compile(r"身分證|身份證|統一編號|national[_ ]?id|^id[_ ]?(no|number)$|ssn", re.I), "TW_NATIONAL_ID"),
    (re.compile(r"address|地址", re.I), "ADDRESS"),
    (re.compile(r"credit|card|信用卡|卡號", re.I), "CREDIT_CARD"),
]

_TAG_TYPE_RE = re.compile(r"<([A-Z_]+)_\d+>")
_WHOLE_TAG_RE = re.compile(r"<[A-Z_]+_\d+>\Z")
_FORMULA_STR_RE = re.compile(r'"((?:[^"]|"")*)"')

# 只對「部門類」欄位作保守的常見名稱豁免。不要全域忽略 POSITION、
# ORGANIZATION 或 PERSON，否則其他欄位的真正個資可能漏掉。
_DEPARTMENT_HEADER_RE = re.compile(
    r"^(?:(?:所屬|員工|工作|負責)?部門(?:名稱)?|科室|科別|組別|"
    r"department|dept|division|team)$", re.I
)
_DEPARTMENT_BASES = (
    "研發", "研究發展", "行政", "人資", "人事", "財務", "會計", "業務",
    "行銷", "市場", "客服", "客戶服務", "資訊", "資訊科技", "資安",
    "工程", "品保", "品管", "品質管理", "營運", "法務", "採購",
    "生產", "製造", "物流", "倉儲", "管理", "總務", "設計",
    "教育訓練", "企劃", "公關", "稽核", "內控", "研發測試",
    "hr", "it", "r&d", "sales", "marketing", "finance",
    "accounting", "operations", "engineering", "support", "design",
)
_DEPARTMENT_SUFFIXES = ("", "部", "處", "科", "室", "組", "中心", "團隊")
# 全字串符合才豁免：「行政部 0912-345-678」絕不會命中。
_SAFE_DEPARTMENT_VALUES = frozenset(
    base + suffix for base in _DEPARTMENT_BASES
    for suffix in (_DEPARTMENT_SUFFIXES if not base.isascii() else ("",))
)


def _is_known_safe_department_name(header, value):
    """僅對明確的部門欄 + 已知非敏感名稱，略過 NER 誤判。

    不放行不認識的值；若該欄任何其他格有 PII，整欄仍需遮蔽。
    這不是全域安全清單，也不改動 core 的 PII/NER 行為。
    """
    if not isinstance(header, str) or not _DEPARTMENT_HEADER_RE.fullmatch(header.strip()):
        return False
    if not isinstance(value, str):
        return False
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    normalized = re.sub(r"\s+", "", normalized)
    return normalized in _SAFE_DEPARTMENT_VALUES


# core.py 的資料夾掃描依賴此全域列表的格式。
LAST_REPORT = []


def _header_type(header):
    header = str(header or "").strip()
    for pattern, etype in HEADER_RULES:
        if pattern.search(header):
            return etype
    return None


def _decode(raw: bytes):
    for encoding in ("utf-8-sig", "cp950"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _is_formula(v):
    return isinstance(v, str) and v.startswith("=")


def _detect_values_for_cell(value):
    """從單一儲存格取待偵測文字。公式不送整段給 NER，以免把函式名當人名。
    只擷取公式中以雙引號包住的文字常數；不能執行 Excel 公式。
    """
    if not isinstance(value, str) or not value.strip():
        return []
    if _WHOLE_TAG_RE.fullmatch(value.strip()):
        return []  # 已遮蔽標籤不是新的 PII 證據
    if _is_formula(value):
        return [m.group(1).replace('""', '"') for m in _FORMULA_STR_RE.finditer(value)
                if m.group(1).strip()]
    return [value]


def _decide_column_types(header, values_by_col, detect_cell, cached_by_col=None):
    """決定每欄是否遮蔽。cache 僅存偵測結果，不改 core 對照表。"""
    cache = {}
    types = []
    report = []
    for i, values in enumerate(values_by_col):
        label = header[i] if i < len(header) else None
        header_type = _header_type(label)
        if header_type:
            types.append(header_type)
            report.append((label, header_type, "表頭規則"))
            continue

        tally = Counter()
        hit_cells = 0
        for row_idx, value in enumerate(values):
            strings = _detect_values_for_cell(value)
            # Excel 公式可能有快取計算結果；額外偵測，但不能依賴它一定存在。
            if cached_by_col is not None and row_idx < len(cached_by_col[i]):
                cached = cached_by_col[i][row_idx]
                if isinstance(cached, str) and cached.strip():
                    strings.extend(_detect_values_for_cell(cached))
            cell_types = set()
            for candidate in strings:
                # 只排除「部門欄」中能明確判斷的常見部門名稱。其他值仍
                # 依舊送 NER 偵測；真正的 PERSON/EMAIL/TW_PHONE 仍會觸發整欄遮蔽。
                if _is_known_safe_department_name(label, candidate):
                    continue
                if candidate not in cache:
                    masked = detect_cell(candidate)
                    cache[candidate] = [tag for tag in _TAG_TYPE_RE.findall(masked or "")
                                        if tag not in IGNORE_TYPES_FOR_COLUMN]
                cell_types.update(cache[candidate])
            if cell_types:
                hit_cells += 1
                tally.update(cell_types)
        if tally:
            etype = tally.most_common(1)[0][0]
            types.append(etype)
            report.append((label, etype, f"內容偵測到個資 ({hit_cells} 格)"))
        else:
            types.append(None)
    return types, report


class _CellMasker:
    def __init__(self, tag_value):
        self.tag_value = tag_value
        self.cache = {}
        self.changed = 0
        self.metadata_removed = 0

    def __call__(self, value, col_type):
        """一整欄所有非空資料格都遮蔽；已是完整標籤的格子保持不變。"""
        if col_type is None or value is None:
            return value
        text = str(value)
        if not text.strip() or _WHOLE_TAG_RE.fullmatch(text.strip()):
            return value
        key = (col_type, text)  # 不 strip：不同空白字元不應合併為相同原文
        if key not in self.cache:
            self.cache[key] = self.tag_value(col_type, text)
        self.changed += 1
        return self.cache[key]


def _check_existing_tag_collisions(values_by_col, col_types):
    """Fail closed on mixed already-masked and raw cells of same tag class.

    core.make_tagger currently calculates its counter from mapping, not the
    incoming table. When mapping is missing older tags, masking another cell
    could silently reuse their IDs. Do not produce a misleading output.
    """
    existing_types = set()
    for col in values_by_col:
        for value in col:
            if isinstance(value, str):
                for typ in _TAG_TYPE_RE.findall(value):
                    existing_types.add(typ)
    if not existing_types:
        return
    new_types = set()
    for i, col in enumerate(values_by_col):
        etype = col_types[i]
        if not etype:
            continue
        for value in col:
            if value is None or not str(value).strip():
                continue
            if not _WHOLE_TAG_RE.fullmatch(str(value).strip()):
                new_types.add(etype)
                break
    conflicted = existing_types & new_types
    if conflicted:
        raise ValueError(
            "表格同時含有既有遮蔽標籤與新的明碼（類型：" +
            ", ".join(sorted(conflicted)) +
            "）。目前 core.make_tagger 不會預留表格中的舊編號，為避免標籤衝突，"
            "已拒絕輸出；請改用未遮蔽的原始表格，或先完成 core 標籤預留功能。"
        )


def _print_report(label, nrows, ncols, report):
    LAST_REPORT.extend(report)
    flagged = [f"{name!r}->{typ} ({why})" for name, typ, why in report]
    print(f"[表格] {label}：{nrows} 列 x {ncols} 欄，整欄遮蔽的欄位：{flagged or '無'}")


def _mask_delimited(path, ext, detect_cell, masker):
    with open(path, "rb") as f:
        text = _decode(f.read())
    delimiter = "\t" if ext == ".tsv" else ","
    if ext == ".csv":
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            pass
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    if not rows:
        return None
    header = rows[0]
    ncols = max(map(len, rows))
    values = [[row[i] if i < len(row) else None for row in rows[1:]] for i in range(ncols)]
    col_types, report = _decide_column_types(header, values, detect_cell)
    _print_report(f"分隔符號={delimiter!r}", len(rows), ncols, report)
    _check_existing_tag_collisions(values, col_types)
    if not any(col_types):
        return None
    out = [header]
    for row in rows[1:]:
        out.append([masker(v, col_types[i]) for i, v in enumerate(row)])
    if masker.changed == 0:
        return None
    out_path = f"{os.path.splitext(path)[0]}_masked{ext}"
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f, delimiter=delimiter).writerows(out)
    return out_path


def _mask_xlsx(path, ext, detect_cell, masker):
    try:
        import openpyxl
        from openpyxl.cell.cell import MergedCell
    except ImportError as e:
        raise RuntimeError("處理 XLSX/XLSM 需要：python -m pip install openpyxl") from e

    # xlsm 必須保留 vbaProject.bin；不要把巨集工作簿當一般 xlsx 重新存檔。
    wb = openpyxl.load_workbook(path, keep_vba=(ext == ".xlsm"), data_only=False, keep_links=True)
    wb_cached = openpyxl.load_workbook(path, keep_vba=False, data_only=True, keep_links=True)
    try:
        # 先檢查整本工作簿，避免前幾個工作表已修改 mapping，
        # 才在後面的工作表發現既有標籤編號衝突。
        plans = []
        all_values_for_collision_check = []
        all_types_for_collision_check = []
        for ws in wb.worksheets:
            if ws.max_row < 1 or ws.max_column < 1:
                continue
            all_rows = list(ws.iter_rows())
            header = [c.value for c in all_rows[0]]
            ncols = ws.max_column
            col_values = [[row[i].value for row in all_rows[1:]] for i in range(ncols)]
            cached_ws = wb_cached[ws.title]
            cached_cols = [
                [cached_ws.cell(r, i + 1).value for r in range(2, ws.max_row + 1)]
                for i in range(ncols)
            ]
            col_types, report = _decide_column_types(header, col_values, detect_cell,
                                                      cached_by_col=cached_cols)
            _print_report(f"工作表 {ws.title!r}", ws.max_row, ncols, report)
            plans.append((ws, all_rows, col_types))
            all_values_for_collision_check.extend(col_values)
            all_types_for_collision_check.extend(col_types)

        _check_existing_tag_collisions(all_values_for_collision_check, all_types_for_collision_check)

        for ws, all_rows, col_types in plans:
            if not any(col_types):
                continue
            changed_before = masker.changed

            # 合併區域任一欄敏感：保護整個合併區域的錨點值。
            # 即使錨點在左邊未被標記的欄，也不能讓該格原文外露。
            merged_anchor_type = {}
            for merged in ws.merged_cells.ranges:
                if merged.max_row < 2:
                    continue
                involved = [col_types[c - 1] for c in range(merged.min_col, merged.max_col + 1)
                            if c - 1 < len(col_types) and col_types[c - 1]]
                if involved:
                    merged_anchor_type[(merged.min_row, merged.min_col)] = involved[0]

            for row in all_rows[1:]:
                for i, cell in enumerate(row):
                    if isinstance(cell, MergedCell):
                        continue
                    etype = col_types[i] or merged_anchor_type.get((cell.row, cell.column))
                    if not etype:
                        continue
                    new = masker(cell.value, etype)
                    if new != cell.value:
                        cell.value = new
                    # 即使儲存格已經是標籤，也不能留註解/超連結中的原始個資。
                    if cell.comment is not None:
                        cell.comment = None
                        masker.metadata_removed += 1
                    if cell.hyperlink is not None:
                        cell.hyperlink = None
                        masker.metadata_removed += 1

            if masker.changed == changed_before:
                print(f"[表格] 工作表 {ws.title!r} 無新增遮蔽值；既有標籤維持不變")

        if masker.changed == 0 and masker.metadata_removed == 0:
            return None
        out_path = f"{os.path.splitext(path)[0]}_masked{ext}"
        wb.save(out_path)
        return out_path
    finally:
        wb.close()
        wb_cached.close()


def mask_file(path, detect_cell, tag_value):
    """現有 core.dispatch_files 呼叫介面不變；無新增遮蔽時回傳 None。"""
    ext = os.path.splitext(path)[1].lower()
    if ext not in TABLE_EXTENSIONS:
        raise ValueError(f"不支援的表格副檔名：{ext}")
    LAST_REPORT.clear()
    masker = _CellMasker(tag_value)
    if ext in (".xlsx", ".xlsm"):
        out = _mask_xlsx(path, ext, detect_cell, masker)
    else:
        out = _mask_delimited(path, ext, detect_cell, masker)
    print(f"[表格] 共遮蔽 {masker.changed} 個儲存格 (相異值 {len(masker.cache)} 種)；"
          f"移除儲存格註解/超連結 {masker.metadata_removed} 項")
    return out
