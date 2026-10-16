"""
程式碼敏感資訊偵測。

偵測來源：
1. 程式碼專屬 Regex：
   API Key、JWT、檔案路徑等。

2. handler_text 的一般 PII Regex：
   電話、Email、信用卡、URL、身分證等。

3. 中文 NER：
   僅允許特定敏感 Entity（例如 PERSON），
   用來偵測程式碼字串中的中文個資。

程式碼模式不執行完整英文 Presidio NLP，
避免程式碼語法被誤判為 LOCATION、PERSON 等 Entity。
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

CODE_ZH_ALLOWED_ENTITIES = {
    "PERSON",
}

def detect(text: str) -> List[Tuple[int, int, str, Optional[float], str]]:
    """
    程式碼敏感資訊偵測：

    1. 程式碼專屬 Regex
    2. handler_text 的一般 PII Regex
    3. 中文 NER（只允許特定敏感 entity）

    不執行英文 Presidio，
    避免程式碼語法被誤判成 LOCATION / PERSON 等。
    """

    results = []

    # ==========================================
    # 1. Code-specific regex
    # ==========================================

    results.extend(
        handler_text._run_rules(
            text,
            _COMPILED
        )
    )

    # ==========================================
    # 2. 一般 PII regex
    # ==========================================

    results.extend(
        handler_text._run_rules(text)
    )

    # ==========================================
    # 3. Chinese NER
    # ==========================================

    zh_results = handler_text.zh_ner(text)

    for match in zh_results:

        start, end, label, score, source = match

        if label not in CODE_ZH_ALLOWED_ENTITIES:
            continue

        results.append(match)

    return results
