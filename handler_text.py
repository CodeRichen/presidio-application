"""
一般文字（非程式碼）敏感資訊偵測規則。
只負責「偵測」，回傳 [(start, end, label), ...]；標籤替換/存檔統一交給 main.py 處理。
想加規則：在 RULES 加一行 Rule(...) 即可，不用動 detect()。
"""
import re
from dataclasses import dataclass
from typing import Callable, Optional, List, Tuple

try:
    from presidio_analyzer import AnalyzerEngine
    _analyzer = AnalyzerEngine()
except Exception:
    _analyzer = None  # 沒裝 presidio / 模型時，自動退化成只用下面的正則規則

EXCLUDED_PRESIDIO_LABELS = {"US_DRIVER_LICENSE"}  # 不想遮蔽的 Presidio 類型，往這個 set 加


def contains_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def luhn_valid(value: str) -> bool:
    """信用卡卡號 Luhn 演算法驗證"""
    digits = [int(d) for d in value if d.isdigit()]
    if not (13 <= len(digits) <= 19):
        return False
    checksum, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


FILE_EXTENSIONS = (
    r"txt|docx?|xlsx?|pptx?|pdf|png|jpe?g|gif|bmp|svg|csv|zip|rar|7z|tar|gz|"
    r"mp4|mp3|wav|py|ipynb|js|ts|jsx|tsx|json|xml|ya?ml|log|ini|cfg|conf|env|sql|html?|css|sh|bat|exe|dll"
)


def quoted_sensitive_valid(value: str) -> bool:
    """給 QUOTED_SECRET 用：引號裡的內容要像網址/檔名/密碼才算數，避免把一般引言也遮掉"""
    if re.match(r"^https?://", value, re.IGNORECASE):
        return True
    if re.search(rf"\.(?:{FILE_EXTENSIONS})$", value, re.IGNORECASE):
        return True
    return (" " not in value and 4 <= len(value) <= 64
            and re.search(r"[A-Za-z]", value) and re.search(r"[0-9]", value))


@dataclass
class Rule:
    name: str
    pattern: str
    flags: int = 0
    validator: Optional[Callable[[str], bool]] = None


RULES = [
    Rule("TW_PHONE", r"\b09\d{2}[- ]?\d{3}[- ]?\d{3}\b"),
    Rule("TW_ID", r"\b[A-Z][12]\d{8}\b"),
    Rule("EMAIL", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    Rule("CREDIT_CARD", r"\b(?:\d[ -]?){13,19}\b", validator=luhn_valid),
    Rule("URL", r"\bhttps?://[^\s\"'<>]+"),
    Rule("FILE_NAME", rf"\b[\w\-. ]+?\.(?:{FILE_EXTENSIONS})\b", flags=re.IGNORECASE),
    Rule("CREDENTIAL_LIKE",
         r"\b(?=[A-Za-z0-9]{8,29}\b)(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{8,29}\b"),
    Rule("GENERIC_SECRET",
         r"\b(?=[A-Za-z0-9_-]{30,}\b)(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{30,}\b"),
    Rule("QUOTED_SECRET", r'["\'“”‘’]([^"\'“”‘’\r\n]{4,100})["\'“”‘’]', validator=quoted_sensitive_valid),
    # 範例：新增自己的規則就照這個格式加一行，例如：
    # Rule("TW_PASSPORT", r"\b\d{9}\b"),
]
_COMPILED = [(r.name, re.compile(r.pattern, r.flags), r.validator) for r in RULES]


def _run_rules(text: str, compiled=None) -> List[Tuple[int, int, str]]:
    """compiled 不給就用本檔的 RULES；handler_code.py 會傳自己的規則進來複用這個函式"""
    compiled = compiled if compiled is not None else _COMPILED
    matches = []
    for name, pattern, validator in compiled:
        for m in pattern.finditer(text):
            if pattern.groups >= 1:
                value, start, end = m.group(1), m.start(1), m.end(1)
            else:
                value, start, end = m.group(0), m.start(), m.end()
            if validator is None or validator(value):
                matches.append((start, end, name))
    return matches


def presidio_matches(text: str) -> List[Tuple[int, int, str]]:
    """單獨跑 Presidio 模型判斷，供 handler_media 這種「已經跑過其他規則」的情境複用"""
    if _analyzer is None or contains_chinese(text):
        return []
    try:
        return [(r.start, r.end, r.entity_type) for r in _analyzer.analyze(text=text, language="en")
                if r.entity_type not in EXCLUDED_PRESIDIO_LABELS]
    except Exception:
        return []  # Presidio 失敗不影響正則規則的結果


def detect(text: str) -> List[Tuple[int, int, str]]:
    """main.py 呼叫的入口：回傳這段文字裡所有敏感資訊的 (start, end, label)"""
    return _run_rules(text) + presidio_matches(text)
