"""離線 handler_csv 功能測試：不會啟動 NER、不修改正式對照表。 
用法：python -m unittest -v test_handler_csv_complete.py
"""
from __future__ import annotations

import csv
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import handler_csv

ROOT = Path(__file__).parent


class Stub:
    """假資料偵測器，測試表格整欄邏輯而非 NER 模型表現。"""
    _email = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    _phone = re.compile(r"09\d{2}-\d{3}-\d{3}")
    _name = re.compile(r"林家宇|陳雅婷|王小明")

    def __init__(self):
        self.tags = {}
        self.mapping = {}
        self.calls = 0

    def detect(self, text):
        self.calls += 1
        if self._email.search(text):
            return "<EMAIL_1>"
        if self._phone.search(text):
            return "<TW_PHONE_1>"
        if self._name.search(text):
            return "<PERSON_1>"
        return None

    def tag_value(self, etype, value):
        if value not in self.tags:
            count = sum(1 for tag in self.tags.values() if tag.startswith(f"<{etype}_")) + 1
            tag = f"<{etype}_{count}>"
            self.tags[value] = tag
            self.mapping[tag] = value
        return self.tags[value]


class CsvCompleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dest = Path(self.temp.name)
        self.stub = Stub()

    def copy(self, filename):
        original = ROOT / "samples" / filename
        path = self.dest / filename
        shutil.copy2(original, path)
        return path

    def mask(self, path):
        return handler_csv.mask_file(str(path), self.stub.detect, self.stub.tag_value)

    def test_csv_any_one_cell_flags_entire_column(self):
        path = self.copy("sample_one_hit.csv")
        original = path.read_bytes()
        out = self.mask(path)
        self.assertTrue(out)
        with open(out, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["編號", "姓名", "備註", "部門"])
        self.assertEqual([r[1] for r in rows[1:]], ["<PERSON_1>", "<PERSON_2>", "<PERSON_3>"])
        self.assertEqual([r[2] for r in rows[1:]], ["<EMAIL_1>", "<EMAIL_2>", "<EMAIL_3>"])
        self.assertEqual([r[3] for r in rows[1:]], ["研發", "行政", "人資"])
        self.assertEqual(path.read_bytes(), original)
        self.assertIn(("備註", "EMAIL", "內容偵測到個資 (1 格)"), handler_csv.LAST_REPORT)

    def test_tsv_one_phone_flags_entire_column(self):
        path = self.copy("sample_one_hit.tsv")
        out = self.mask(path)
        with open(out, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f, delimiter="\t"))
        self.assertTrue(all(r[1].startswith("<") for r in rows[1:]))
        self.assertEqual(rows[1][2], "核對")

    def test_no_pii_no_output(self):
        path = self.dest / "safe.csv"
        path.write_text("部門,備註\n研發,已完成\n行政,正常\n", encoding="utf-8")
        self.assertIsNone(self.mask(path))
        self.assertFalse((self.dest / "safe_masked.csv").exists())

    def test_no_double_masking_if_column_already_tags(self):
        path = self.dest / "already.csv"
        path.write_text("姓名,部門\n<PERSON_1>,工程\n<PERSON_2>,人資\n", encoding="utf-8")
        self.assertIsNone(self.mask(path))
        self.assertEqual(self.stub.mapping, {})

    def test_xlsx_formula_and_multi_sheet(self):
        import openpyxl
        path = self.copy("sample_person_column.xlsx")
        out = self.mask(path)
        self.assertTrue(out)
        wb = openpyxl.load_workbook(out)
        try:
            s = wb["員工資料"]
            self.assertEqual(s["A2"].value, 1)
            self.assertEqual(s["D2"].value, "研發")
            self.assertEqual(s["B2"].value, "<PERSON_1>")
            self.assertEqual(s["B5"].value, "<PERSON_4>")
            self.assertEqual(s["C2"].value, "<EMAIL_1>")
            self.assertEqual(s["C3"].value, "<EMAIL_2>")
            self.assertEqual(s["E2"].value, "=1+1")
            x = wb["公式內容偵測"]
            self.assertRegex(x["B2"].value, r"^<PERSON_\d+>$")
            self.assertRegex(x["B3"].value, r"^<PERSON_\d+>$")
            self.assertEqual(x["C3"].value, "待辦")
            merged = wb["合併儲存格"]
            self.assertRegex(merged["A2"].value, r"^<PERSON_\d+>$")
            self.assertEqual(merged["D2"].value, "正常")
            self.assertEqual(len(merged.merged_cells.ranges), 1)
        finally:
            wb.close()

    def test_xlsx_formula_in_sensitive_header_is_masked(self):
        import openpyxl
        path = self.copy("sample_person_column.xlsx")
        out = self.mask(path)
        wb = openpyxl.load_workbook(out, data_only=False)
        try:
            self.assertFalse(wb["員工資料"]["B5"].value.startswith("="))
        finally:
            wb.close()

    def test_xlsx_removes_comments_and_hyperlinks_in_flagged_column(self):
        import openpyxl
        from openpyxl.comments import Comment
        path = self.copy("sample_person_column.xlsx")
        wb = openpyxl.load_workbook(path)
        ws = wb["員工資料"]
        ws["B2"].comment = Comment("原始姓名和電話", "測試")
        ws["B3"].hyperlink = "https://example.com/private?name=person"
        wb.save(path)
        wb.close()
        out = self.mask(path)
        masked = openpyxl.load_workbook(out)
        try:
            self.assertIsNone(masked["員工資料"]["B2"].comment)
            self.assertIsNone(masked["員工資料"]["B3"].hyperlink)
        finally:
            masked.close()

    def test_mixed_old_and_new_person_tags_fail_closed(self):
        path = self.dest / "mixed.csv"
        path.write_text("姓名,部門\n<PERSON_1>,研發\n林家宇,行政\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "標籤衝突"):
            self.mask(path)
        self.assertFalse((self.dest / "mixed_masked.csv").exists())

    def test_xlsx_cross_sheet_tag_conflict_rejected_before_tagging(self):
        import openpyxl
        path = self.copy("sample_person_column.xlsx")
        wb = openpyxl.load_workbook(path)
        ws = wb.create_sheet("已有標籤")
        ws["A1"] = "其他"
        ws["A2"] = "<PERSON_1>"
        wb.save(path)
        wb.close()
        with self.assertRaisesRegex(ValueError, "標籤衝突"):
            self.mask(path)
        self.assertEqual(self.stub.mapping, {})
        self.assertFalse((self.dest / "sample_person_column_masked.xlsx").exists())

    def test_xlsx_only_tag_with_comment_still_removes_metadata(self):
        import openpyxl
        from openpyxl.comments import Comment
        path = self.copy("sample_person_column.xlsx")
        wb = openpyxl.load_workbook(path)
        ws = wb["員工資料"]
        for row in range(2, 6):
            ws.cell(row, 2).value = f"<PERSON_{row-1}>"
        # remove all other potentially flagged columns/sheets for this case
        ws["C3"].value = "一般內容"
        wb.remove(wb["公式內容偵測"])
        wb.remove(wb["合併儲存格"])
        ws["B2"].comment = Comment("這裡包含敏感資訊", "測試")
        wb.save(path)
        wb.close()
        out = self.mask(path)
        self.assertTrue(out)
        final = openpyxl.load_workbook(out)
        try:
            self.assertIsNone(final["員工資料"]["B2"].comment)
            self.assertEqual(final["員工資料"]["B2"].value, "<PERSON_1>")
        finally:
            final.close()

    def test_xlsm_mode_opens_keep_vba(self):
        import openpyxl
        from unittest.mock import patch
        # 此為無巨集的檔案，僅測試副檔名與 keep_vba 參數；真實含巨集檔需額外驗收。
        src = self.copy("sample_person_column.xlsx")
        path = src.with_suffix(".xlsm")
        src.rename(path)
        original_load = openpyxl.load_workbook
        calls = []
        def spy(*args, **kwargs):
            calls.append(kwargs)
            return original_load(*args, **kwargs)
        with patch.object(openpyxl, "load_workbook", side_effect=spy):
            out = self.mask(path)
        self.assertTrue(out.endswith(".xlsm"))
        self.assertTrue(any(call.get("keep_vba") is True for call in calls))


if __name__ == "__main__":
    unittest.main()
