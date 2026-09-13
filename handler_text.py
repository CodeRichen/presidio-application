"""
一般文字（非程式碼）敏感資訊偵測規則。
只負責「偵測」，回傳 [(start, end, label), ...]；標籤替換/存檔統一交給 main.py 處理。
想加規則：在 RULES 加一行 Rule(...) 即可，不用動 detect()。

偵測分三層：
    1) 正則規則 (RULES，不分語言都會跑)
    2) 不管文字是中是英，一律先跑一次 Presidio 的英文規則式辨識器 (en_core_web_sm)
       —— Presidio 內建的 SSN/IBAN/信用卡/加密貨幣錢包/IP 這類 pattern-based 辨識器
       幾乎都只註冊在 supported_language="en"，用 language="zh" 呼叫是完全不會觸發的，
       所以不管內容含不含中文都要跑這一段，不然這些內建規則等於白裝
    3) 偵測到中文，另外疊加一次中文的 CKIP NER (只判斷 PERSON/LOCATION/ORGANIZATION)
       (zh_core_web_sm 只拿來給 Presidio 做斷詞，NER 結果刻意不採用，避免跟 CKIP 重複判斷、互相打架；
        zh_core_web_sm 本身中文 NER 品質也比 CKIP 弱，兩個一起跑只會增加雜訊)
第 2、3 層是同一個 AnalyzerEngine，實際呼叫的函式在 NLP_MODELS 這個註冊表裡。
想整個換掉某一層（例如換別的中文 NER 模型、接雲端 API），寫一個 func(text) -> [(start,end,label), ...]，
把 NLP_MODELS["zh"] 或 NLP_MODELS["en"] 蓋掉即可，presidio_matches()/detect() 都不用動。
"""
import re
from dataclasses import dataclass
from typing import Callable, Optional, List, Tuple
import os
os.environ["HF_HUB_OFFLINE"] = "1"          # huggingface_hub 完全離線
os.environ["TRANSFORMERS_OFFLINE"] = "1"    # transformers 也跟著離線
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"  # 順手關掉遙測回報

MIN_ZH_NER_SCORE = 0.7  # CKIP 判斷的信心分數門檻，低於這個值直接丟掉不採用；想放寬/收緊調這個數字即可

try:
    from presidio_analyzer import AnalyzerEngine, EntityRecognizer, RecognizerResult, RecognizerRegistry
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from transformers import pipeline

    # ---- 1. 中/英文的基礎 NLP 引擎 (spaCy)：中文模型只拿來斷詞，NER 判斷交給下面的 CKIP 模型 ----
    nlp_configuration = {
        "nlp_engine_name": "spacy",
        "models": [
            {"lang_code": "zh", "model_name": "zh_core_web_sm"},  
            {"lang_code": "en", "model_name": "en_core_web_trf"}, # 這邊從sm改掉會有幫助嗎?
        ],
    }
    _nlp_engine = NlpEngineProvider(nlp_configuration=nlp_configuration).create_engine()

    # ---- 2. 自訂中文 NER 辨識器：只用 Hugging Face 的 ckiplab/bert-base-chinese-ner 判斷人名/地名/機構名 ----
    class CustomHfChineseRecognizer(EntityRecognizer):
        def __init__(self):
            super().__init__(supported_entities=["PERSON", "LOCATION", "ORGANIZATION"],
                              supported_language="zh")
            # print("正在載入 Hugging Face 中文 NER 模型 (ckiplab/bert-base-chinese-ner)...")
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
    _chinese_recognizer = None
    try:
        _chinese_recognizer = CustomHfChineseRecognizer()  # 只建立一次，不管走哪個分支都重複使用，避免重複載入模型
    except Exception as e:
        print(f"[警告] 中文 NER 模型載入失敗，中文只會用英文那組規則式辨識器：{e}")

    try:
        # supported_languages 一定要跟下面 AnalyzerEngine 給的一致，不然 Presidio 會直接丟例外
        _registry = RecognizerRegistry(supported_languages=["en", "zh"])
        _registry.load_predefined_recognizers(languages=["en"])
        if _chinese_recognizer:
            _registry.add_recognizer(_chinese_recognizer)
        _analyzer = AnalyzerEngine(nlp_engine=_nlp_engine, registry=_registry, supported_languages=["zh", "en"])
        # print("Presidio AnalyzerEngine 建立成功，已掛載中文 CKIP NER 模型")
    except Exception as e:
        # 萬一 Presidio 版本的 registry API 不一樣，退回舊做法(中文會跟 spaCy 內建 NER 重複判斷，但至少能跑)
        print(f"[警告] 自訂 registry 建立失敗，改用預設設定：{e}")
        _analyzer = AnalyzerEngine(nlp_engine=_nlp_engine, supported_languages=["zh", "en"])
        if _chinese_recognizer:
            try:
                _analyzer.registry.add_recognizer(_chinese_recognizer)
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
        # print("Presidio分析結果:")
        # for r in _analyzer.analyze(text=text, language=lang):
            # print(r.start, r.end, r.entity_type, round(float(r.score), 2))
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


def iban_valid(value: str) -> bool:
    """IBAN 的 ISO 7064 MOD 97-10 檢查碼驗證，格式寬鬆比對+這個驗證可以大幅降低誤判"""
    v = re.sub(r"\s+", "", value).upper()
    if not re.match(r"^[A-Z]{2}\d{2}[A-Z0-9]{4,30}$", v):
        return False
    rearranged = v[4:] + v[:4]
    try:
        numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    except ValueError:
        return False
    return int(numeric) % 97 == 1


@dataclass
class Rule:
    name: str
    pattern: str
    flags: int = 0
    validator: Optional[Callable[[str], bool]] = None


# 注意：這裡故意不用 \b 當邊界，因為 Python 的 \b 是以 \w (含中文字!) 定義的，
# 中文字緊貼著英數字時 (例如「陳先生sk-xxx」中間沒有空格) \b 會判斷錯誤的起點，
# 導致抓到的字串從中間某個標點斷開、開頭少一截。改用 (?<![A-Za-z0-9_]) / (?![A-Za-z0-9_])
# 明確只把「前後緊接著英數字/底線」視為同一個詞的延續，中文字一律當作邊界，才不會切錯。
_L = r"(?<![A-Za-z0-9_])"  # 左邊界
_R = r"(?![A-Za-z0-9_])"   # 右邊界

RULES = [
    Rule("TW_PHONE", _L + r"09\d{2}[- ]?\d{3}[- ]?\d{3}" + _R),
    Rule("TW_ID", _L + r"[A-Z][12]\d{8}" + _R),
    Rule("EMAIL", _L + r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}" + _R),
    Rule("CREDIT_CARD", _L + r"(?:\d[ -]?){13,19}" + _R, validator=luhn_valid),
    Rule("URL", _L + r"https?://[^\s\"'<>]+"),
    Rule("FILE_NAME", _L + rf"[\w\-. ]+?\.(?:{FILE_EXTENSIONS})" + _R, flags=re.IGNORECASE),
    Rule("CREDENTIAL_LIKE",
         _L + r"(?=[A-Za-z0-9]{8,29}(?![A-Za-z0-9]))(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{8,29}" + _R),
    Rule("GENERIC_SECRET",
         _L + r"(?=[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-]))(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{30,}" + _R),
    Rule("QUOTED_SECRET", r'["\'“”‘’]([^"\'“”‘’\r\n]{4,100})["\'“”‘’]', validator=quoted_sensitive_valid),

    # ---- 以下是實際測試後補上的規則：涵蓋台灣/國際常見格式，Presidio 沒有內建或準確率不夠 ----
    Rule("MAC_ADDRESS", _L + r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}" + _R),
    Rule("IP_ADDRESS", _L + r"(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)" + _R),
    Rule("IBAN", _L + r"[A-Z]{2}\d{2}(?:[ ]?[A-Za-z0-9]{2,4}){3,8}" + _R, validator=iban_valid),
    Rule("SWIFT_CODE", _L + r"[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?" + _R),
    Rule("VEHICLE_VIN", _L + r"[A-HJ-NPR-Z0-9]{17}" + _R),
    Rule("US_SSN", _L + r"\d{3}-\d{2}-\d{4}" + _R),
    Rule("US_PHONE", _L + r"\+1[ ]?\(\d{3}\)[ ]?\d{3}-\d{4}" + _R),
    Rule("GPS_COORDINATES", r"\d{1,3}\.\d+°\s*[NS]\s*,\s*\d{1,3}\.\d+°\s*[EW]"),
    Rule("DATE_OF_BIRTH", r"\d{4}年\d{1,2}月\d{1,2}日"),  # 中文年月日格式，也可能是其他日期，非專指生日
    Rule("MEDICAL_RECORD_NUMBER", _L + r"MRN-\d{4}-\d{6}" + _R),
    Rule("TAX_ID", r"統一編號\s*\d{8}"),           # 用中文標籤字詞當上下文錨點，降低純8碼數字的誤判
    Rule("INSURANCE_ID", r"健保投保序號\s*[A-Z]\d{10}"),  # 同上，錨定標籤文字才抓，格式太泛用容易誤判
    Rule("DRIVER_LICENSE", _L + r"[A-Z]\d{7}" + _R),      # 格式很泛用(1字母+7位數)，容易跟其他代碼誤判，可自行收緊
    Rule("LICENSE_PLATE", _L + r"[A-Z]{3}-\d{4}" + _R),   # 只涵蓋「英英英-數數數數」這種車牌格式，其他格式要自己加
    Rule("BANK_ACCOUNT", _L + r"\d{3,4}-\d{3,4}-\d{6,9}" + _R),
    # 台灣地址(市/縣 + 區/鄉/鎮 + 路/街/大道 + 段 + 號 + 樓)：屬於結構性最佳嘗試，非窮舉所有地址寫法
    Rule("ADDRESS",
         r"[\u4e00-\u9fff]{2,4}(?:市|縣)[\u4e00-\u9fff]{2,4}(?:區|鄉|鎮)"
         r"[\u4e00-\u9fff0-9]{2,10}(?:路|街|大道)(?:[一二三四五六七八九十0-9]{1,3}段)?"
         r"\d{1,4}號(?:\d{1,3}樓)?"),
    # 範例：新增自己的規則就照這個格式加一行，例如：
    # Rule("TW_PASSPORT", _L + r"\d{9}" + _R),
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
    """先永遠跑一次英文那組 (Presidio 內建的規則式辨識器如 SSN/IBAN/信用卡/加密貨幣錢包/IP
    幾乎都只註冊在 language="en" 底下，不管文字裡有沒有中文都要跑，不然這些規則等於形同虛設)，
    偵測到中文再疊加一次中文的 CKIP NER 判斷人名/地名/機構名"""
    matches = NLP_MODELS["en"](text)
    if contains_chinese(text):
        matches += NLP_MODELS["zh"](text)
    return matches


def detect(text: str) -> List[Tuple[int, int, str, float]]:
    """main.py 呼叫的入口：回傳這段文字裡所有敏感資訊的 (start, end, label, score)，
    score 是 Presidio 判定的信心分數 (0~1)，正則規則比對到的固定是 None"""
    return _run_rules(text) + presidio_matches(text)