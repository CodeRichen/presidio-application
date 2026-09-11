"""
一般文字（非程式碼）敏感資訊偵測規則。
只負責「偵測」，回傳 [(start, end, label), ...]；標籤替換/存檔統一交給 main.py 處理。
想加規則：在 RULES 加一行 Rule(...) 即可，不用動 detect()。

偵測分三層：
    1) 正則規則 (RULES，不分語言都會跑)
    2) 英文內容 -> Presidio 內建的英文模型 (en_core_web_sm) + Presidio 內建規則式辨識器
    3) 中文內容 -> 只用掛載的 Hugging Face ckiplab/bert-base-chinese-ner 判斷 PERSON/LOCATION/ORGANIZATION
       (zh_core_web_sm 只拿來給 Presidio 做斷詞，NER 結果刻意不採用，避免跟 CKIP 重複判斷、互相打架；
        zh_core_web_sm 本身中文 NER 品質也比 CKIP 弱，兩個一起跑只會增加雜訊)
第 2、3 層是同一個 AnalyzerEngine，用哪個語言是依 contains_chinese() 判斷後在 NLP_MODELS
這個註冊表裡選函式。想整個換掉中文那層（例如換別的中文 NER 模型、接雲端 API），
寫一個 func(text) -> [(start,end,label), ...]，把 NLP_MODELS["zh"] 蓋掉即可，其他地方不用動。
"""
import re
from dataclasses import dataclass
from typing import Callable, Optional, List, Tuple

MIN_ZH_NER_SCORE = 0.7  # CKIP 判斷的信心分數門檻，低於這個值直接丟掉不採用；想放寬/收緊調這個數字即可

try:
    from presidio_analyzer import AnalyzerEngine, EntityRecognizer, RecognizerResult, RecognizerRegistry
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from transformers import pipeline

    # ---- 1. 中/英文的基礎 NLP 引擎 (spaCy)：中文模型只拿來斷詞，NER 判斷交給下面的 CKIP 模型 ----
    nlp_configuration = {
        "nlp_engine_name": "spacy",
        "models": [
            {"lang_code": "zh", "model_name": "zh_core_web_sm"},  # python -m spacy download zh_core_web_sm
            {"lang_code": "en", "model_name": "en_core_web_sm"},
        ],
    }
    _nlp_engine = NlpEngineProvider(nlp_configuration=nlp_configuration).create_engine()

    # ---- 2. 自訂中文 NER 辨識器：只用 Hugging Face 的 ckiplab/bert-base-chinese-ner 判斷人名/地名/機構名 ----
    class CustomHfChineseRecognizer(EntityRecognizer):
        def __init__(self):
            super().__init__(supported_entities=["PERSON", "LOCATION", "ORGANIZATION"],
                              supported_language="zh")
            print("正在載入 Hugging Face 中文 NER 模型 (ckiplab/bert-base-chinese-ner)...")
            self.ner_pipeline = pipeline("ner", model="ckiplab/bert-base-chinese-ner",
                                          aggregation_strategy="simple")

        def load(self):
            pass

        def analyze(self, text, entities, nlp_artifacts=None):
            label_map = {"PER": "PERSON", "LOC": "LOCATION", "ORG": "ORGANIZATION"}
            results = []
            for item in self.ner_pipeline(text):
                entity_type = label_map.get(item["entity_group"])
                score = float(item["score"])
                if entity_type and score >= MIN_ZH_NER_SCORE and (not entities or entity_type in entities):
                    results.append(RecognizerResult(entity_type=entity_type, start=item["start"],
                                                      end=item["end"], score=score))
            return results

    # ---- 3. 手動組 registry：英文照 Presidio 內建規則正常載入；中文只掛 CKIP 這一個辨識器，
    #          不讓 Presidio 自動載入 zh_core_web_sm 內建的 NER 辨識器，避免跟 CKIP 重複判斷 ----
    try:
        _registry = RecognizerRegistry()
        _registry.load_predefined_recognizers(languages=["en"])
        _registry.add_recognizer(CustomHfChineseRecognizer())
        _analyzer = AnalyzerEngine(nlp_engine=_nlp_engine, registry=_registry, supported_languages=["zh", "en"])
    except Exception as e:
        # 萬一 Presidio 版本的 registry API 不一樣，退回舊做法(中文會跟 spaCy 內建 NER 重複判斷，但至少能跑)
        print(f"[警告] 自訂 registry 建立失敗，改用預設設定：{e}")
        _analyzer = AnalyzerEngine(nlp_engine=_nlp_engine, supported_languages=["zh", "en"])
        try:
            _analyzer.registry.add_recognizer(CustomHfChineseRecognizer())
        except Exception:
            pass
except Exception:
    _analyzer = None  # 完全沒裝 presidio/transformers 時，自動退化成只用下面的正則規則

EXCLUDED_PRESIDIO_LABELS = {"US_DRIVER_LICENSE"}  # 不想遮蔽的 Presidio 類型，往這個 set 加


def contains_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _presidio_call(text: str, lang: str) -> List[Tuple[int, int, str, float]]:
    if _analyzer is None:
        return []
    try:
        return [(r.start, r.end, r.entity_type, round(float(r.score), 2))
                for r in _analyzer.analyze(text=text, language=lang)
                if r.entity_type not in EXCLUDED_PRESIDIO_LABELS]
    except Exception:
        return []  # 該語言模型沒裝/分析失敗都不影響正則規則的結果


def presidio_en(text: str) -> List[Tuple[int, int, str, float]]:
    return _presidio_call(text, "en")


def presidio_zh(text: str) -> List[Tuple[int, int, str, float]]:
    return _presidio_call(text, "zh")


# 語言 -> NLP 模型函式 的註冊表：想加/換某個語言用的模型，往這裡改一行就好
NLP_MODELS = {
    "en": presidio_en,
    "zh": presidio_zh,
}


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


def _run_rules(text: str, compiled=None) -> List[Tuple[int, int, str, float]]:
    """compiled 不給就用本檔的 RULES；handler_code.py 會傳自己的規則進來複用這個函式
    正則規則沒有「信心分數」的概念，統一補 None，跟 Presidio 的結果格式對齊方便後面合併"""
    compiled = compiled if compiled is not None else _COMPILED
    matches = []
    for name, pattern, validator in compiled:
        for m in pattern.finditer(text):
            if pattern.groups >= 1:
                value, start, end = m.group(1), m.start(1), m.end(1)
            else:
                value, start, end = m.group(0), m.start(), m.end()
            if validator is None or validator(value):
                matches.append((start, end, name, None))
    return matches


def presidio_matches(text: str) -> List[Tuple[int, int, str, float]]:
    """依語言挑對應的 NLP 模型跑（英文/中文各自的 Presidio 模型），供 handler_media 這種
    「已經跑過其他規則」的情境複用"""
    lang = "zh" if contains_chinese(text) else "en"
    model_func = NLP_MODELS.get(lang)
    return model_func(text) if model_func else []


def detect(text: str) -> List[Tuple[int, int, str, float]]:
    """main.py 呼叫的入口：回傳這段文字裡所有敏感資訊的 (start, end, label, score)，
    score 是 Presidio 判定的信心分數 (0~1)，正則規則比對到的固定是 None"""
    return _run_rules(text) + presidio_matches(text)