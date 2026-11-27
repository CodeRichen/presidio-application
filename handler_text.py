"""
一般文字（非程式碼）敏感資訊偵測規則。
只負責「偵測」，回傳 [(start, end, label, score, source), ...]；標籤替換/存檔統一交給 main.py 處理。
想加規則：在 RULES 加一行 Rule(...) 即可，不用動 detect()。
固定要遮的人名/機構名等：寫進 known_entities.txt (每行 LABEL<Tab>值)，detect() 會當成 "regex" 規則一起跑。

第 5 個欄位 source 是這筆結果的「來源」，固定是下面三種之一：
    "regex" -> 正則規則命中 (score 固定 None)
    "en"    -> Presidio 英文模型 (環境裡的 en_core_web_sm，或 models/en_core_web_sm；都沒有就停用) + Presidio 內建規則式辨識器
    "zh"    -> 中文 NER (A: CLUENER + C: bert-base-multilingual-cased-ner-hrl + Repeat，由 ner_ac_engine.py 整併；
               A 的來源是環境的 Hugging Face 快取或 models/roberta-base-finetuned-cluener2020-chinese)
main.py / handler_media.py 在合併重疊結果時，就是靠這個欄位決定「規則 > 語言吻合的模型 > 語言
不吻合的模型」的優先序，所以每個偵測函式都要老實標好 source，不能省略。

偵測分三層：
    1) 正則規則 (RULES，不分語言都會跑)
    2) 已知實體清單 (known_entities.txt，當成 regex 來源)
    3) NLP 模型 (NLP_MODELS：英文走 Presidio，中文走 zh_ner)
想換模型：寫一個 func(text) -> [(start,end,label,score,source), ...]，
把 NLP_MODELS["zh"] 或 NLP_MODELS["en"] 蓋掉即可，presidio_matches()/detect() 都不用動；
source 欄位固定回傳 "en" 或 "zh"（對應蓋掉的是哪一層），重疊優先序才不會判斷錯。

環境變數 PII_NER_BACKEND=a 可退回只用 A (CLUENER)；預設 ac = A+C+Repeat。
"""
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional, List, Tuple

import app_env  # 判斷環境有沒有模型、models 資料夾位置 (APP_DIR/models)；打包成 EXE 後也能正確找到 EXE 旁邊的模型

# 中文文字中不採用英文 spaCy 容易誤判的語意型實體
EN_NER_LABELS = {"PERSON", "ORGANIZATION", "LOCATION"}

# 啟動時各模型的載入結果，core.warmup() 會把這些訊息印到記錄區，讓使用者知道哪些偵測功能目前停用
MODEL_STATUS = []

# 模型尋找順序：① 先查「環境」(pip 裝好的 spaCy 模型 / 本機 Hugging Face 快取) → ② 再看 models/ 資料夾 → ③ 都沒有就停用
EN_MODEL_PACKAGE = "en_core_web_sm"                                   # 環境裡的 spaCy 模型套件名稱
EN_MODEL_DIR_NAME = "en_core_web_sm"                                  # models/ 底下的資料夾名稱
ZH_MODEL_DIR_NAME = "roberta-base-finetuned-cluener2020-chinese"      # models/ 底下的資料夾名稱

# ---------- 英文：走 Presidio，含內建的規則式辨識器 (SSN/IBAN/信用卡/加密貨幣錢包/IP) + spaCy 英文 NER ----------
_analyzer = None
_en_sources = []                                   # [(來源說明, spaCy 模型名稱或資料夾路徑), ...] 依優先序嘗試
if app_env.has_spacy_package(EN_MODEL_PACKAGE):
    _en_sources.append(("環境已安裝", EN_MODEL_PACKAGE))
_en_model_dir = app_env.find_model_dir(EN_MODEL_DIR_NAME)
if _en_model_dir is not None:
    _en_sources.append(("models 資料夾", _en_model_dir))

if not _en_sources:
    MODEL_STATUS.append(f"[停用] 英文 NER (Presidio)：環境沒有 {EN_MODEL_PACKAGE}，"
                        f"{os.path.join(app_env.MODELS_DIR, EN_MODEL_DIR_NAME)} 也找不到，"
                        "英文的人名/地點/機構不會被偵測 (正則規則照常運作)")
else:
    _en_errors = []
    for _label, _model in _en_sources:
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            _nlp_engine = NlpEngineProvider(nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": _model}],  # 只需要英文，中文不用 spaCy
            }).create_engine()
            _analyzer = AnalyzerEngine(nlp_engine=_nlp_engine, supported_languages=["en"])
            MODEL_STATUS.append(f"[啟用] 英文 NER (Presidio + spaCy {EN_MODEL_PACKAGE})，來源：{_label}")
            break
        except Exception as e:           # 環境裡的壞掉 / presidio 沒包進程式時，換下一個來源試試
            _analyzer = None
            _en_errors.append(f"{_label}：{e}")
    else:
        MODEL_STATUS.append("[停用] 英文 NER (Presidio) 載入失敗，只用正則規則 -> " + "；".join(_en_errors))

EXCLUDED_PRESIDIO_LABELS = {"US_DRIVER_LICENSE"}  # 不想遮蔽的 Presidio 類型，往這個 set 加


def contains_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def presidio_en(text: str) -> List[Tuple[int, int, str, Optional[float], str]]:
    if _analyzer is None:
        return []
    try:
        return [(r.start, r.end, r.entity_type, round(float(r.score), 2), "en")
                for r in _analyzer.analyze(text=text, language="en")
                if r.entity_type not in EXCLUDED_PRESIDIO_LABELS]
    except Exception:
        return []  # 模型沒裝/分析失敗都不影響正則規則的結果


# ---------- 中文 A：CLUENER，不經過 Presidio/spaCy，直接呼叫 Hugging Face 模型 ----------
ZH_NER_MODEL_NAME = "uer/roberta-base-finetuned-cluener2020-chinese"  # 環境快取的名稱、prepare_models.py 下載用
MIN_ZH_NER_SCORE = 0.7  # 信心分數門檻，低於這個值直接丟掉不採用

# CLUENER2020 標籤 -> 實體名稱。book/game/movie/scene 與隱私無關，故意不對應、不遮蔽。
_ZH_LABEL_MAP = {
    "name": "PERSON",
    "address": "ADDRESS",
    "company": "ORGANIZATION",
    "government": "ORGANIZATION",
    "organization": "ORGANIZATION",
    "position": "POSITION",
}
_zh_ner_pipeline = None
_zh_sources = []                                   # [(來源說明, 模型名稱或資料夾路徑), ...] 依優先序嘗試
if app_env.has_hf_cached(ZH_NER_MODEL_NAME):
    _zh_sources.append(("環境 (Hugging Face 快取)", ZH_NER_MODEL_NAME))
_zh_model_dir = app_env.find_model_dir(ZH_MODEL_DIR_NAME)
if _zh_model_dir is not None:
    _zh_sources.append(("models 資料夾", _zh_model_dir))

if not _zh_sources:
    MODEL_STATUS.append(f"[停用] 中文 NER (A)：環境沒有 {ZH_NER_MODEL_NAME}，"
                        f"{os.path.join(app_env.MODELS_DIR, ZH_MODEL_DIR_NAME)} 也找不到，"
                        "中文的人名/地址/機構只靠 C 與正則規則")
else:
    try:
        from transformers import pipeline
    except ModuleNotFoundError as e:
        pipeline = None
        MODEL_STATUS.append(f"[停用] 中文 NER (A)：沒有安裝 transformers 套件：{e}")
    except Exception as e:
        pipeline = None
        MODEL_STATUS.append(f"[停用] 中文 NER (A)：transformers 匯入失敗 (常見原因是 torch 版本不合)：{e}")
    if pipeline is not None:
        _zh_errors = []
        for _label, _model in _zh_sources:
            try:
                print(f"正在載入中文 NER 模型 ({_label}：{_model})...")
                _zh_ner_pipeline = pipeline("ner", model=_model, aggregation_strategy="simple")
                MODEL_STATUS.append(f"[啟用] 中文 NER (A: {ZH_NER_MODEL_NAME})，來源：{_label}")
                break
            except Exception as e:       # 快取不完整 / 損壞時，換下一個來源試試
                _zh_ner_pipeline = None
                _zh_errors.append(f"{_label}：{e}")
        else:
            MODEL_STATUS.append("[停用] 中文 NER (A) 載入失敗 -> " + "；".join(_zh_errors))


def _zh_ner_a(text: str) -> List[Tuple[int, int, str, Optional[float], str]]:
    """A：直接呼叫中文 NER pipeline。長文字切成帶 overlap 的區塊推論，再把座標換回原文。
    只回傳分數 >= MIN_ZH_NER_SCORE 的結果。"""
    if _zh_ner_pipeline is None or not text:
        return []

    chunk_size, overlap = 350, 50      # 實測約 400 字可推論、702 字會失敗；350 字一段、重疊 50 字
    step = chunk_size - overlap
    matches = []
    try:
        for chunk_start in range(0, len(text), step):
            chunk_end = min(chunk_start + chunk_size, len(text))
            for item in _zh_ner_pipeline(text[chunk_start:chunk_end]):
                label = _ZH_LABEL_MAP.get(item["entity_group"])
                score = float(item["score"])
                if label and score >= MIN_ZH_NER_SCORE:
                    matches.append((chunk_start + item["start"], chunk_start + item["end"],
                                    label, round(score, 2), "zh"))
            if chunk_end >= len(text):
                break
    except Exception as e:
        print(f"[警告] 中文 NER 分析失敗：{e}")
        return []

    dedup = {}                          # overlap 區域同一實體可能重複，相同 span 保留分數較高者
    for m in matches:
        key = (m[0], m[1], m[2], m[4])
        if key not in dedup or (m[3] or 0) > (dedup[key][3] or 0):
            dedup[key] = m
    return sorted(dedup.values(), key=lambda m: (m[0], m[1], m[2]))


# ---------- 中文 A + C + Repeat (ner_ac_engine.py) ----------
_AC_ENABLED = False
_ac_warned = False
if os.environ.get("PII_NER_BACKEND", "ac").strip().lower() == "a":
    MODEL_STATUS.append("[設定] 中文 NER：僅 A (PII_NER_BACKEND=a)")
else:
    try:
        import ner_ac_engine as _ac
        _AC_ENABLED = True
        if _ac.warmup():
            MODEL_STATUS.append("[啟用] 中文 NER：A + C (bert-base-multilingual-cased-ner-hrl) + Repeat")
        else:
            MODEL_STATUS.append("[停用] 中文 NER 的 C 模型 (找不到或不完整)，目前僅 A + 後處理")
    except ImportError as e:
        MODEL_STATUS.append(f"[停用] 找不到 ner_ac_engine.py，中文僅用 A：{e}")


def zh_ner(text: str) -> List[Tuple[int, int, str, Optional[float], str]]:
    """中文 NER 入口 (handler_code.py 也直接用這個)：A 的結果交給 ner_ac_engine 整併；
    整併出錯時退回 A 的原始結果，不影響正則規則與其他層。"""
    global _ac_warned
    a_rows = _zh_ner_a(text)
    if not _AC_ENABLED or not text:
        return a_rows
    try:
        return _ac.ensemble_from_a(text, a_rows)
    except Exception as e:
        if not _ac_warned:
            print(f"[警告] A+C+Repeat 整併失敗，改用 A 的結果：{e}")
            _ac_warned = True
        return a_rows


# 語言 -> NLP 模型函式 的註冊表：想加/換某個語言用的模型，往這裡改一行就好
NLP_MODELS = {
    "en": presidio_en,
    "zh": zh_ner,
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
    """IBAN 的 ISO 7064 MOD 97-10 檢查碼驗證"""
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


# 不用 \b 當邊界：Python 的 \b 以 \w (含中文字) 定義，中文字緊貼英數字時會切錯起點。
# 改用 (?<![A-Za-z0-9_]) / (?![A-Za-z0-9_])，只把前後緊接的英數字/底線視為同一個詞的延續。
_L = r"(?<![A-Za-z0-9_])"  # 左邊界
_R = r"(?![A-Za-z0-9_])"   # 右邊界

RULES = [
    Rule("TW_PHONE", _L + r"09\d{2}[- ]?\d{3}[- ]?\d{3}" + _R),
    Rule("TW_ID", _L + r"[A-Z][12]\d{8}" + _R),
    Rule("EMAIL", _L + r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}" + _R),
    Rule("CREDIT_CARD", _L + r"(?:\d[ -]?){13,19}" + _R, validator=luhn_valid),
    Rule("URL", _L + r"https?://[^\s\"'<>]+"),
    Rule("FILE_NAME", _L + rf"[\w\-. ]+?\.(?:{FILE_EXTENSIONS})(?!\s*\()" + _R, flags=re.IGNORECASE),
    Rule("CREDENTIAL_LIKE",
         _L + r"(?=[A-Za-z0-9]{8,29}(?![A-Za-z0-9]))(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{8,29}" + _R),
    Rule("GENERIC_SECRET",
         _L + r"(?=[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-]))(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{30,}" + _R),
    Rule("QUOTED_SECRET", r'["\'“”‘’]([^"\'“”‘’\r\n]{4,100})["\'“”‘’]', validator=quoted_sensitive_valid),

    # ---- 台灣/國際常見格式，Presidio 沒有內建或準確率不夠 ----
    Rule("MAC_ADDRESS", _L + r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}" + _R),
    Rule("IP_ADDRESS", _L + r"(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)" + _R),
    Rule("IBAN", _L + r"[A-Z]{2}\d{2}(?:[ ]?[A-Za-z0-9]{2,4}){3,8}" + _R, validator=iban_valid),
    Rule("SWIFT_CODE", _L + r"[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?" + _R),
    Rule("VEHICLE_VIN", _L + r"[A-HJ-NPR-Z0-9]{17}" + _R),
    Rule("US_SSN", _L + r"\d{3}-\d{2}-\d{4}" + _R),
    Rule("US_PHONE", _L + r"\+1[ ]?\(\d{3}\)[ ]?\d{3}-\d{4}" + _R),
    Rule("GPS_COORDINATES", r"\d{1,3}\.\d+°\s*[NS]\s*,\s*\d{1,3}\.\d+°\s*[EW]"),
    Rule("DATE_OF_BIRTH", r"\d{4}年\d{1,2}月\d{1,2}日"),  # 中文年月日格式，也可能是其他日期，非專指生日
    # ---- 醫療 / 保險 / 理賠識別碼：使用明確前綴，避免大量誤判 ----
    Rule("LICENSE_NUMBER", _L + r"TWMD-\d{7}" + _R),
    Rule("MEDICAL_RECORD_NUMBER", _L + r"(?:MRN-\d{4}-\d{6}|CGH-\d{8}-\d{4})" + _R),
    Rule("INSURANCE_POLICY_NUMBER", _L + r"POL-TW-\d{7}" + _R),
    Rule("EMPLOYEE_ID", _L + r"EMP-\d{5}" + _R),
    Rule("CASE_NUMBER", _L + r"CLM-\d{4}-\d{7}" + _R),

    Rule("TAX_ID", r"(?:統一編號|统一编号)\s*\d{8}"),                      # 以中文標籤當上下文錨點，降低純 8 碼數字誤判
    Rule("INSURANCE_ID", r"(?:健保投保序號|健保投保序号)\s*[A-Z]\d{10}"),  # 同上
    Rule("DRIVER_LICENSE", _L + r"[A-Z]\d{7}" + _R),      # 格式泛用 (1 字母 + 7 位數)，容易與其他代碼誤判
    Rule("LICENSE_PLATE", _L + r"[A-Z]{3}-\d{4}" + _R),   # 只涵蓋「英英英-數數數數」格式
    Rule("BANK_ACCOUNT", _L + r"\d{3,4}-\d{3,4}-\d{6,9}" + _R),
    # 台灣地址 (市/縣 + 區/鄉/鎮 + 路/街/大道 + 段 + 號 + 樓)：結構性最佳嘗試
    Rule("ADDRESS",
         r"[\u4e00-\u9fff]{2,4}(?:市|縣)[\u4e00-\u9fff]{2,4}(?:區|鄉|鎮)"
         r"[\u4e00-\u9fff0-9]{2,10}(?:路|街|大道)(?:[一二三四五六七八九十0-9]{1,3}段)?"
         r"\d{1,4}號(?:\d{1,3}樓)?"),
    # 新增規則照格式加一行，例如：Rule("TW_PASSPORT", _L + r"\d{9}" + _R),
]
_COMPILED = [(r.name, re.compile(r.pattern, r.flags), r.validator) for r in RULES]


def _run_rules(text: str, compiled=None) -> List[Tuple[int, int, str, Optional[float], str]]:
    """compiled 不給就用本檔的 RULES；handler_code.py 會傳自己的規則進來複用這個函式。
    正則規則沒有信心分數，統一補 None；來源固定標記 "regex"。"""
    compiled = compiled if compiled is not None else _COMPILED
    matches = []
    for name, pattern, validator in compiled:
        for m in pattern.finditer(text):
            if pattern.groups >= 1:
                value, start, end = m.group(1), m.start(1), m.end(1)
            else:
                value, start, end = m.group(0), m.start(), m.end()
            if validator is None or validator(value):
                matches.append((start, end, name, None, "regex"))
    return matches


def presidio_matches(text: str) -> List[Tuple[int, int, str, Optional[float], str]]:
    """英文那組 (含 Presidio 內建規則式辨識器) 永遠跑；含中文時，丟掉英文 spaCy 容易誤判的
    人名/機構/地點，改疊加中文 NER (A+C+Repeat)。"""
    en_matches = NLP_MODELS["en"](text)
    if contains_chinese(text):
        en_matches = [m for m in en_matches if m[2] not in EN_NER_LABELS]
        en_matches += NLP_MODELS["zh"](text)
    return en_matches


# ---------- 已知實體清單 (known_entities.txt)：手動維護，格式 LABEL<Tab>值 ----------
KNOWN_ENTITIES_FILE = "./presidio/real/known_entities.txt"
_LABEL_OK = re.compile(r"[A-Z_]+")


def load_known_entities(path=None):
    """讀 known_entities.txt -> {"PERSON": {"邢立承", ...}, ...}。檔案不存在就回傳空 dict。"""
    path = path or KNOWN_ENTITIES_FILE
    known = {}
    try:
        with open(path, encoding="utf-8-sig") as f:      # utf-8-sig：記事本存檔的 BOM 也能讀
            for n, line in enumerate(f, 1):
                line = line.rstrip("\r\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                label, sep, value = line.partition("\t")
                label, value = label.strip().upper(), value.strip()
                if not sep or not value or not _LABEL_OK.fullmatch(label):
                    print(f"[警告] {path} 第 {n} 行格式不符 (需為 LABEL<Tab>值)，已略過")
                    continue
                known.setdefault(label, set()).add(value)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"[警告] 讀取 {path} 失敗：{e}")
    return known


def _alt(value):
    """re.escape 之外，英數開頭/結尾的值加邊界判斷，避免 Tom 命中 Tomorrow 的一部分"""
    p = re.escape(value)
    if re.match(r"[A-Za-z0-9]", value[0]):
        p = r"(?<![A-Za-z0-9])" + p
    if re.match(r"[A-Za-z0-9]", value[-1]):
        p += r"(?![A-Za-z0-9])"
    return p


def build_known_patterns(known):
    """每個 label 編一個 alternation pattern；值依長度由長到短排序，避免短字串蓋掉長字串"""
    return {
        label: re.compile("|".join(_alt(v) for v in sorted(vals, key=len, reverse=True)))
        for label, vals in known.items() if vals
    }


def _known_mtime():
    try:
        return os.path.getmtime(KNOWN_ENTITIES_FILE)
    except OSError:
        return None


KNOWN = load_known_entities()
KNOWN_PATTERNS = build_known_patterns(KNOWN)
_known_loaded_mtime = _known_mtime()
if KNOWN:
    print(f"[已知實體] 已載入 {sum(len(v) for v in KNOWN.values())} 筆 ({', '.join(KNOWN)})")


def reload_known_entities():
    """重新讀檔並重編 pattern (長時間執行的服務改了清單後不用重開程式)"""
    global KNOWN, KNOWN_PATTERNS, _known_loaded_mtime
    KNOWN = load_known_entities()
    KNOWN_PATTERNS = build_known_patterns(KNOWN)
    _known_loaded_mtime = _known_mtime()
    print(f"[已知實體] 重新載入，共 {sum(len(v) for v in KNOWN.values())} 筆")


def detect_known_entities(text):
    """回傳 [(start, end, label, None, "regex")]：source 用 "regex" (優先序最高)"""
    if _known_mtime() != _known_loaded_mtime:    # 檔案被改過就自動重載
        reload_known_entities()
    return [(m.start(), m.end(), label, None, "regex")
            for label, pattern in KNOWN_PATTERNS.items() for m in pattern.finditer(text)]


def detect(text: str) -> List[Tuple[int, int, str, Optional[float], str]]:
    """main.py 呼叫的入口：回傳 (start, end, label, score, source)；
    score 為模型信心分數 (正則為 None)，source 為 "regex"/"en"/"zh" 三選一。"""
    return _run_rules(text) + detect_known_entities(text) + presidio_matches(text)