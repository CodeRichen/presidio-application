"""
程式碼敏感資訊偵測規則：API Key、JWT、檔案路徑等。
程式碼裡也常混著電話/信用卡/URL/密碼這類個資，所以 detect() 會借用 handler_text 的規則一起跑，
但不跑 Presidio 模型（程式碼上下文用不太到、也容易誤判），所以這裡回傳的每一筆 source 都固定是
"regex"（詳見 handler_text._run_rules 的說明）。
想加規則：在 RULES 加一行 Rule(...) 即可。
"""
import re
from dataclasses import dataclass
from typing import Callable, Optional, List, Tuple

import handler_text  # 複用 handler_text 的 _run_rules() 與個資 RULES，避免重複維護一樣的規則


@dataclass
class Rule:
    name: str
    pattern: str
    flags: int = 0
    validator: Optional[Callable[[str], bool]] = None


RULES = [
    Rule("FILE_PATH", r"(?:[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)*[^\\/:*?\"<>|\r\n]+)|(?:/(?:[^/\s]+/)+[^/\s]+)"),
    Rule("JWT", handler_text._L + r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+" + handler_text._R),
    Rule("API_KEY_OPENAI", handler_text._L + r"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}" + handler_text._R),
    Rule("API_KEY_AWS", handler_text._L + r"(?:AKIA|ASIA)[0-9A-Z]{16}" + handler_text._R),
    Rule("API_KEY_GITHUB", handler_text._L + r"(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}" + handler_text._R),
    Rule("API_KEY_GOOGLE", handler_text._L + r"AIza[0-9A-Za-z\-_]{35}" + handler_text._R),
    # 範例：新增自己的規則就照這個格式加一行，例如：
    # Rule("DB_CONN_STRING", r"(?:postgres|mysql|mongodb)://[^\s\"']+"),
]
_COMPILED = [(r.name, re.compile(r.pattern, r.flags), r.validator) for r in RULES]


def detect(text: str) -> List[Tuple[int, int, str, Optional[float], str]]:
    """main.py 呼叫的入口：程式碼專屬規則 + handler_text 的個資規則一起跑。
    回傳 (start, end, label, score, source)，這裡全部都是純正則比對，score 固定 None、
    source 固定 "regex"（不跑 Presidio/中文 NER，見檔頭說明）。"""
    return handler_text._run_rules(text, _COMPILED) + handler_text._run_rules(text)
