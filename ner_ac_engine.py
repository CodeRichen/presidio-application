"""離線 A+C+Repeat 研究模型引擎（不覆蓋團隊主程式）。

介面：detect(text) -> [(start, end, label, score, source), ...]
  source = regex / en / zh；所有座標均為原始 Python 字串 [start, end)。

預設 C_LONG_TEXT_MODE="chunk"：延續目前啟用的 C 專屬長文本分塊。
可設為 "chunk"：只有 C 超過 tokenizer 上限時才做分塊掃描。
不依賴 handler_text_v3.py / handler_text_v32.py / compare_three_ner_fixed.py
或 handler_text_ac_repeat.py / long_text_ner_patch.py / handler_text_c_chunked.py。

注意：模型需已下載到本機；本檔不在網路上下載模型。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Optional, List, Tuple, Sequence, Iterable, Any, Mapping

# 在首次載入 transformers/huggingface 之前設定，禁止自動連網。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# baseline: 原來使用的 A+C+Repeat；chunk: C 超長輸入分塊（目前版本）。
C_LONG_TEXT_MODE = os.environ.get("PII_C_LONG_MODE", "chunk").strip().lower()
if C_LONG_TEXT_MODE not in ("baseline", "chunk"):
    raise ValueError("PII_C_LONG_MODE 必須是 baseline 或 chunk")

C_CHUNK_TOKENS = 400
C_OVERLAP_TOKENS = 40
NER_DEBUG = False  # True 時會印出含原始姓名的模型輸出，正式使用請保持 False。


EN_NER_LABELS = {
    "PERSON",
    "ORGANIZATION",
    "LOCATION",
}


COMPOUND_SURNAMES = {
    "歐陽",
    "司馬",
    "上官",
    "諸葛",
    "夏侯",
    "東方",
    "皇甫",
    "尉遲",
    "公孫",
    "慕容",
    "司徒",
    "司空",
    "令狐",
    "長孫",
    "宇文",
    "軒轅",
}


BUSINESS_SUFFIXES = tuple(sorted({
    "建築事務所", "運動用品", "物流中心",
    "自行車行", "文化協會", "音樂教室",
    "陶藝工坊", "陶藝坊", "食品行",
    "水電行", "機車行", "印刷社",
    "旅行社", "健身房", "健身中心",
    "手作坊", "攝影工作室", "攝影",
    "裝潢", "花藝", "美甲", "茶行",
    "茶館", "書店", "診所", "旅宿",
    "農場", "建材", "麵包坊",
}, key=len, reverse=True))


CLUENER_MODEL_NAME = "uer/roberta-base-finetuned-cluener2020-chinese"


MIN_CLUENER_SCORE = 0.7


_ZH_LABEL_MAP = {
    "name": "PERSON",
    "address": "ADDRESS",
    "company": "ORGANIZATION",
    "government": "ORGANIZATION",
    "organization": "ORGANIZATION",
    "position": "POSITION",
}


C_MODEL_ID = "Davlan/bert-base-multilingual-cased-ner-hrl"
THIRD_NER_SCORE = 0.95
_ckip_pipeline = None  # 已移除 CKIP 載入；保留舊介面檢查相容性。
_cluener_pipeline = None
_c_pipeline = None
_c_tokenizer = None

@lru_cache(maxsize=1)
def _get_c_pipeline():
    global _c_pipeline, _c_tokenizer
    from transformers import pipeline, AutoTokenizer, AutoModelForTokenClassification
    # Team app_env already enforces offline mode and supports packaged models/.
    import app_env
    candidates = []
    if app_env.has_hf_cached(C_MODEL_ID):
        candidates.append(C_MODEL_ID)
    packaged = app_env.find_model_dir('bert-base-multilingual-cased-ner-hrl')
    if packaged is not None:
        candidates.append(packaged)
    if not candidates:
        raise OSError('找不到離線 C 模型（Hugging Face 快取或 models/bert-base-multilingual-cased-ner-hrl）')
    errors = []
    for candidate in candidates:
        try:
            tokenizer = AutoTokenizer.from_pretrained(candidate, local_files_only=True, use_fast=True)
            model = AutoModelForTokenClassification.from_pretrained(candidate, local_files_only=True)
            break
        except (OSError, ValueError) as exc:
            errors.append(f'{candidate}: {exc}')
    else:
        raise OSError('C 模型快取不完整：' + '; '.join(errors))
    _c_tokenizer = tokenizer
    _c_pipeline = pipeline(
        "ner", model=model, tokenizer=_c_tokenizer, aggregation_strategy="simple")
    return _c_pipeline

def third_ner(text: str) -> list[dict]:
    """C 模型原始輸出，格式與 transformers NER pipeline 相同。"""
    if not text:
        return []
    predictor = _get_c_pipeline()
    # TokenClassificationPipeline 預設會依 tokenizer.model_max_length 截斷。
    return list(predictor(text))


def expand_compound_surname(text, matches):
    """
    中文複姓 PERSON 邊界補全。

    1. 修正模型只辨識部分姓名的情況。
    2. 在明確人物語境下，補充完全漏判的複姓。
    3. 避免將複姓商號直接當成人名。

    matches:
        (start, end, label, score, source)
    """

    # 人物語境：刻意採用較保守的規則
    PERSON_PREFIXES = (
        "請", "通知", "聯繫", "邀請",
        "交給", "由", "找",
    )

    PERSON_SUFFIXES = (
        "確認", "簽收", "出席", "負責",
        "協助", "回覆", "處理",
    )

    def is_chinese(value):
        return bool(re.fullmatch(r"[\u4e00-\u9fff]+", value))

    def is_business_context(start, end):

        candidate = text[start:end]
        after = text[end:]

        # 1. 候選文字本身是否含有商號後綴
        if any(
            candidate.endswith(suffix)
            for suffix in BUSINESS_SUFFIXES
        ):
            return True

        # 2. 候選文字後方是否緊接商號後綴
        if any(
            after.startswith(suffix)
            for suffix in BUSINESS_SUFFIXES
        ):
            return True

        # 3. 是否與 NER 辨識的公司實體重疊
        for entity_start, entity_end, label, score, source in matches:

            if label not in ("ORG", "ORGANIZATION", "COMPANY"):
                continue

            if start < entity_end and end > entity_start:
                return True

        return False

    results = list(matches)

    # =====================================
    # 第一階段：補全既有 PERSON
    # =====================================

    for surname in COMPOUND_SURNAMES:

        for surname_match in re.finditer(
            re.escape(surname), text
        ):
            surname_start = surname_match.start()
            name_start = surname_start
            given_start = surname_start + 2

            # 嘗試補全為「複姓 + 兩字名字」
            name_end = given_start + 2

            if name_end > len(text):
                continue

            candidate = text[name_start:name_end]

            if not is_chinese(candidate):
                continue

            if is_business_context(name_start, name_end):
                continue

            # 模型是否已辨識候選姓名的任何一部分
            related = [
                match
                for match in results
                if match[2] == "PERSON"
                and match[0] < name_end
                and match[1] > name_start
            ]

            if not related:
                continue

            # 若已存在完整辨識，就不必修改
            if any(
                start == name_start and end == name_end
                for start, end, label, score, source
                in related
            ):
                continue

            # 移除候選姓名內的部分 PERSON
            results = [
                match
                for match in results
                if not (
                    match[2] == "PERSON"
                    and match[0] >= name_start
                    and match[1] <= name_end
                )
            ]

            best_score = max(
                (match[3] or 0)
                for match in related
            )

            results.append(
                (
                    name_start,
                    name_end,
                    "PERSON",
                    best_score,
                    "COMPOUND_SURNAME_EXPANSION"
                )
            )

    # =====================================
    # 第二階段：保守補充完全漏判姓名
    # =====================================

    for surname in COMPOUND_SURNAMES:

        for surname_match in re.finditer(
            re.escape(surname), text
        ):
            start = surname_match.start()
            end = start + 4

            if end > len(text):
                continue

            candidate = text[start:end]

            if not is_chinese(candidate):
                continue

            if is_business_context(start, end):
                continue

            # 不要覆蓋已存在的 PERSON
            if any(
                label == "PERSON"
                and person_start < end
                and person_end > start
                for person_start, person_end, label, score, source
                in results
            ):
                continue

            before = text[:start]
            after = text[end:]

            has_person_prefix = any(
                before.endswith(prefix)
                for prefix in PERSON_PREFIXES
            )

            has_person_suffix = any(
                after.startswith(suffix)
                for suffix in PERSON_SUFFIXES
            )

            # 需要前後文共同支持
            if not (
                has_person_prefix
                and has_person_suffix
            ):
                continue

            results.append(
                (
                    start,
                    end,
                    "PERSON",
                    0.75,
                    "COMPOUND_SURNAME_CONTEXT"
                )
            )

    return sorted(
        results,
        key=lambda m: (m[0], m[1], m[2])
    )


def resolve_overlapping_person_spans(matches):
    """
    解決 PERSON span 完全包含的問題。

    例如：
        (0, 4) PERSON -> 東方奕辰
        (2, 4) PERSON -> 奕辰

    最後只保留：
        (0, 4) PERSON -> 東方奕辰
    """

    # 優先處理長度較長的 PERSON
    sorted_matches = sorted(
        matches,
        key=lambda m: (
            -(m[1] - m[0]),
            -(m[3] if m[3] is not None else 0),
            m[0]
        )
    )

    selected = []

    for candidate in sorted_matches:
        start, end, label, score, source = candidate

        # 非 PERSON 不做重疊處理
        if label != "PERSON":
            selected.append(candidate)
            continue

        is_contained = False

        for existing in selected:
            ex_start, ex_end, ex_label, _, _ = existing

            if ex_label != "PERSON":
                continue

            # 已保留的 PERSON 完全包含目前 PERSON
            if ex_start <= start and end <= ex_end:
                is_contained = True
                break

        if not is_contained:
            selected.append(candidate)

    return sorted(
        selected,
        key=lambda m: (m[0], m[1], m[2])
    )


def filter_person_in_organization_context(text, matches):
    """
    過濾公司、商店名稱中被誤判的 PERSON。

    僅處理：
        PERSON 後面緊接商業名稱後綴的情況。

    例如：
        雨柔服飾 -> 移除「雨柔」
        品妤工作室 -> 移除「品妤」

    不處理：
        周承翰在工作室 -> 保留「周承翰」
    """

    ORG_SUFFIXES = {
        "企業",
        "公司",
        "有限公司",
        "股份有限公司",
        "科技",
        "資訊",
        "工程",
        "建設",
        "設計",
        "服飾",
        "餐飲",
        "咖啡",
        "文具",
        "工作室",
        "書局",
        "出版社",
        "早餐店",
        "花店",
        "汽車",
        "電器",
        "眼鏡",
        "百貨",
    }

    filtered = []

    for match in matches:
        start, end, label, score, source = match

        if label != "PERSON":
            filtered.append(match)
            continue

        # PERSON 後面的文字
        remaining = text[end:]

        # 排除容易與商業後綴混淆的職稱
        PERSON_TITLE_PREFIXES = {
            "工程師",
            "設計師",
        }

        is_person_title = any(
            remaining.startswith(title)
            for title in PERSON_TITLE_PREFIXES
        )

        is_org_name = (
            not is_person_title
            and any(
                remaining.startswith(suffix)
                for suffix in ORG_SUFFIXES
            )
        )

        if is_org_name:
            continue

        filtered.append(match)

    return filtered


def correct_person_title_boundary(text, matches):
    """
    修正 NER 將「姓氏 + 職稱」誤判為 PERSON 的情況。

    例如：
    張律師 -> 張
    郭課長 -> 郭

    不影響：
    郭品妤經理 -> 郭品妤
    彭書瑋工程師 -> 彭書瑋
    """

    SURNAMES = set(
        "王李張劉陳楊黃趙周吳徐孫朱馬胡郭"
        "林何高羅鄭謝曾彭許洪蕭葉鍾廖杜"
        "范宋唐邱蔡賴蘇潘呂江戴方丁"
    )

    TITLES = (
        "工程師", "設計師", "護理師",
        "教授", "老師", "醫師", "律師",
        "經理", "主任", "校長", "博士",
        "先生", "小姐",
        "組長", "處長", "總監", "院長",
        "秘書", "副理", "課長",
    )

    corrected = []

    for start, end, label, score, source in matches:

        if label != "PERSON":
            corrected.append(
                (start, end, label, score, source)
            )
            continue

        entity_text = text[start:end]

        # 完整匹配「單字姓氏 + 職稱」
        is_surname_title = (
            len(entity_text) >= 2
            and entity_text[0] in SURNAMES
            and entity_text[1:] in TITLES
        )

        if is_surname_title:
            corrected.append(
                (
                    start,
                    start + 1,
                    "PERSON",
                    score,
                    "PERSON_TITLE_BOUNDARY"
                )
            )
        else:
            corrected.append(
                (start, end, label, score, source)
            )

    return corrected


def filter_business_person_false_positives(text, results):
    """
    移除商號名稱內被誤判為 PERSON 的 span。

    results:
        [(start, end, label, score, source), ...]

    保留非 PERSON 的實體。
    保留與商號無關的真實姓名。
    """

    filtered = []

    for result in results:
        start, end, label = result[:3]

        if label != "PERSON":
            filtered.append(result)
            continue

        person_text = text[start:end]

        # 只針對常見的 2~4 字姓名候選做判斷
        if not (2 <= len(person_text) <= 4):
            filtered.append(result)
            continue

        # 檢查 PERSON 後面緊接的原始文字
        right_context = text[end:]

        is_business = any(
            right_context.startswith(suffix)
            for suffix in BUSINESS_SUFFIXES
        )

        if is_business:
            continue

        filtered.append(result)

    return filtered


def detect_surname_with_title(text, matches):
    """
    偵測「單字姓氏 + 職稱」，補足 NER 漏判。

    改善：
    1. 避免辨識「最高經理」「曾經理論」等非姓名文字
    2. 避免與現有 PERSON 結果重疊
    3. 支援更多常見職稱
    4. 保留原本五元組格式

    回傳：
    (start, end, label, score, source)
    """

    SURNAMES = set(
        "王李張劉陳楊黃趙周吳徐孫朱馬胡郭"
        "林何高羅鄭謝曾彭許洪蕭葉鍾廖杜"
        "范宋唐邱蔡賴蘇潘呂江戴方丁"
    )

    TITLES = (
        "工程師", "設計師", "護理師",
        "教授", "老師", "醫師", "律師",
        "經理", "主任", "校長", "博士",
        "先生", "小姐",
        "組長", "處長", "總監", "院長",
        "秘書", "副理", "課長",
    )

    # 常見誤判詞彙
    FALSE_POSITIVE_PHRASES = {
        "最高經理",
        "曾經理論",
        "周末老師",
    }

    title_pattern = "|".join(
        re.escape(title)
        for title in sorted(TITLES, key=len, reverse=True)
    )

    pattern = re.compile(
        rf"([{''.join(sorted(SURNAMES))}])({title_pattern})"
    )

    results = list(matches)

    for match in pattern.finditer(text):

        start = match.start(1)
        end = match.end(1)

        # ======================================
        # 1. 排除已知誤判詞彙
        # ======================================

        is_false_positive = False

        for phrase in FALSE_POSITIVE_PHRASES:

            phrase_start = text.find(phrase)

            while phrase_start != -1:

                phrase_end = phrase_start + len(phrase)

                if phrase_start <= start < phrase_end:
                    is_false_positive = True
                    break

                phrase_start = text.find(
                    phrase,
                    phrase_start + 1
                )

            if is_false_positive:
                break

        if is_false_positive:
            continue

        # ======================================
        # 2. 排除已經被 NER 辨識的 PERSON
        # ======================================

        already_detected = any(
            label == "PERSON"
            and person_start <= start
            and end <= person_end

            for person_start, person_end, label, score, source
            in results
        )

        if already_detected:
            continue

        # ======================================
        # 3. 避免在完整姓名內部額外辨識姓氏
        # ======================================

        overlaps_existing_person = any(
            label == "PERSON"
            and start < person_end
            and end > person_start

            for person_start, person_end, label, score, source
            in results
        )

        if overlaps_existing_person:
            continue

        # ======================================
        # 4. 新增 PERSON
        # ======================================

        results.append(
            (
                start,
                end,
                "PERSON",
                1.0,
                "SURNAME_TITLE_RULE"
            )
        )

    return sorted(
        results,
        key=lambda m: (m[0], m[1], m[2])
    )


NON_NAME_ENDINGS = ("辩护", "辯護", "律师", "律師", "教授", "记者", "記者", "主席", "总理", "總理", "市长", "市長")


TEMPORAL_FOLLOWERS = ("表示", "指出", "强调", "強調", "说", "說", "称", "稱", "提到", "认为", "認為")


GEO_CONTEXT_PATTERN = re.compile(
    r"友好(?:合作)?(?:关系|關係|交往)|外交关系|外交關係|停火协议|停火協議|"
    r"国际关系|國際關係|省市|边境|邊境|领土|領土|城市交流"
)


COMMON_ROLE_WORDS = {"保姆", "保母"}


def _person_candidate(text, item, min_score=THIRD_NER_SCORE):
    label = str(item.get("entity_group", "")).upper()
    if label not in ("PER", "PERSON"):
        return None
    score = float(item.get("score", 0))
    if score < min_score:
        return None
    start, end = int(item["start"]), int(item["end"])
    if start < 0 or end > len(text) or start >= end:
        return None
    # The title/role check must run on the *whole* raw candidate, not only
    # before replacement, to prevent '伊藤辩护' from becoming a fake surname.
    candidate = text[start:end]
    if not (2 <= len(candidate) <= 12):
        return None
    if not any("\u4e00" <= ch <= "\u9fff" for ch in candidate):
        return None
    if any(ch.isspace() for ch in candidate):
        return None
    if candidate in COMMON_ROLE_WORDS:
        return None
    if candidate.endswith(NON_NAME_ENDINGS):
        return None
    return start, end, "PERSON", score, "zh"


def _gpe_supports_location(start, end, ckip_raw):
    """Require >= 80% of the candidate to be covered by high-confidence GPE.

    CKIP often splits one GPE into multiple adjacent token groups.
    """
    if not ckip_raw:
        return False
    covered = set()
    for item in ckip_raw:
        if str(item.get("entity_group", "")).upper() not in ("GPE", "LOC", "LOCATION"):
            continue
        if float(item.get("score", 0)) < 0.95:
            continue
        s = int(item["start"])
        e = int(item["end"])
        covered.update(range(max(start, s), min(end, e)))
    return len(covered) >= 0.80 * (end - start)


def _strong_person_context(text, start, end):
    left = text[max(0, start - 12):start]
    right = text[end:end + 25]
    if right.startswith(("政府", "政權", "政权")) and any(
        verb in right for verb in ("辞职", "辭職", "被迫", "下台", "任命", "卸任")
    ):
        return True
    if right.startswith(("卫冕", "衛冕", "夺冠", "奪冠", "赢得", "贏得", "表示", "指出", "说", "說", "称", "稱")):
        return True
    if any(left.endswith(t) for t in ("记者", "記者", "教授", "選手", "选手", "总统", "總統", "拳击赛", "拳擊賽", "部长", "部長")):
        return True
    return False


def _likely_location_in_context(text, start, end, ckip_raw):
    # Strong geographical *context* plus an independent GPE prediction;
    # not simply 'CKIP says GPE' (that would discard real foreign names).
    after = text[end:end + 70]
    before = text[max(0, start - 15):start]
    geo_context = bool(GEO_CONTEXT_PATTERN.search(after))
    geo_context |= after.startswith(("地区", "地區", "省", "县", "縣", "自治区", "自治區"))
    geo_context |= before.endswith(("位于", "位於", "抵达", "抵達")) and after.startswith(("市", "地区", "地區"))
    return geo_context and _gpe_supports_location(start, end, ckip_raw) and not _strong_person_context(text, start, end)


def _extension_is_safe(text, old, candidate):
    """Permit two narrowly-scoped kinds of PERSON boundary completion.

    1. Append 1-2 missing final characters (e.g. 居来提·土尔 -> 居来提·土尔地).
    2. Recover the *prefix* of a short tail-only prediction (e.g. 思 -> 马克思).

    No simultaneous left/right expansion and no change to an unrelated span.
    """
    os, oe = old[0], old[1]
    ns, ne = candidate[0], candidate[1]

    # Case 2: CLUENER/CKIP retained only a 1-2 character PERSON suffix.
    # The independent third NER model supplies a complete PER containing it.
    if ns < os and ne == oe:
        candidate_text = text[ns:ne]
        prefix = text[ns:os]
        return (
            len(text[os:oe]) <= 2
            and 2 <= len(candidate_text) <= 8
            and 1 <= len(prefix) <= 6
            and all("\u4e00" <= ch <= "\u9fff" for ch in prefix)
            and all("\u4e00" <= ch <= "\u9fff" for ch in text[os:oe])
        )

    # Case 1: right-only extension, unchanged from V3.1.
    if ns != os or ne <= oe or ne - oe > 2:
        return False
    extra = text[oe:ne]
    if any(ch.isspace() for ch in extra):
        return False
    if any(ch in "，。；：！？（）《》“”\"'、" for ch in extra):
        return False
    if extra in ("时", "時") and text[ne:].startswith(TEMPORAL_FOLLOWERS):
        return False
    if extra in ("的", "了", "在", "和", "与", "與", "为", "為", "地") and "·" not in text[ns:ne]:
        return False
    return True


def merge_v32(text: str, original_results: Sequence[tuple], third_raw: Sequence[dict], ckip_raw=None, min_score=THIRD_NER_SCORE):
    """Pure function for local regression tests, with no ML imports."""
    merged = list(original_results)
    persons = [r for r in merged if r[2] == "PERSON"]

    for item in sorted(third_raw, key=lambda x: (int(x["start"]), int(x["end"]))):
        candidate = _person_candidate(text, item, min_score)
        if candidate is None:
            continue
        s, e, *_ = candidate
        overlaps = [p for p in persons if s < p[1] and e > p[0]]
        if overlaps:
            # Avoid replacing two adjacent independent names by one long span.
            if len(overlaps) != 1:
                continue
            old = overlaps[0]
            if not (s <= old[0] and e >= old[1]):
                continue
            if not _extension_is_safe(text, old, candidate):
                continue
            # Right-edge extension may add grammatical material; demand stronger support.
            # Suffix-only restoration is a different case: the previous model
            # simply missed the left boundary, so use the normal 0.95 PER cutoff.
            is_left_recovery = s < old[0] and e == old[1]
            required_score = 0.95 if is_left_recovery else 0.98
            if candidate[3] < required_score:
                continue
            merged.remove(old)
            persons.remove(old)
        else:
            if _likely_location_in_context(text, s, e, ckip_raw):
                continue
        merged.append(candidate)
        persons.append(candidate)

    # Ensure no duplicated PERSON spans caused by the third model.
    unique = {}
    for r in merged:
        k = (r[0], r[1], r[2])
        if k not in unique or float(r[3] or 0) > float(unique[k][3] or 0):
            unique[k] = r
    return sorted(unique.values(), key=lambda r: (r[0], r[1], r[2]))


SURNAMES = set(
    '趙錢孫李周吳鄭王馮陳褚衛蔣沈韓楊朱秦尤許何呂施張孔曹嚴華金魏陶姜戚謝鄒喻柏水竇章'
    '雲蘇潘葛奚范彭郎魯韋昌馬苗鳳花方俞任袁柳鮑史唐費廉岑薛雷賀倪湯滕殷羅畢郝鄔安常'
    '樂于時傅皮卞齊康伍余元卜顧孟平黃和穆蕭尹姚邵湛汪祁毛禹狄米貝明臧計伏成戴談宋'
    '茅龐熊紀舒屈項祝董梁杜阮藍閔席季麻強賈路婁危江童顏郭梅盛林刁鍾徐邱駱高夏蔡田'
    '樊胡凌霍虞萬支柯昝管盧莫經房裘繆干解應宗丁宣賁鄧郁單杭洪包諸左石崔吉鈕龔程嵇'
    '邢滑裴陸榮翁荀羊甄家封芮羿儲靳汲邴糜松井段富巫烏焦巴弓牧隗山谷車侯宓蓬全郗'
    '班仰秋仲伊宮寧仇欒暴甘鈄厲戎祖武符劉景詹束龍葉幸司黎薄印宿白懷蒲邰從鄂索咸'
    '籍賴卓藺屠蒙池喬陰胥能蒼雙聞莘黨翟譚貢勞逄姬申扶堵冉宰酈雍郤璩桑桂濮牛壽'
    '通邊扈燕冀郟浦尚農溫別莊晏柴瞿閻充慕連茹習宦艾魚容向古易慎戈廖庾終暨居衡步'
    '都耿滿弘匡國文寇廣祿闕東毆殳沃利蔚越夔隆師鞏厙聶晁勾敖融冷訾辛闞那簡饒空曾'
    '毋沙乜養鞠須豐巢關蒯相查后荊紅游竺權逯蓋益桓公仉督岳帥緱亢況郈有琴商牟佘佴'
    '伯賞南門呼延歸海羊舌微生岳帥'
)


PREFIX = re.compile(
    r'(?:我是|本人(?:是|為)?|(?:我的|他的|她的)?(?:姓名|名字)(?:是|為|叫|[:：])?|'
    r'聯絡人(?:是|為|[:：])?|申請人(?:是|為|[:：])?|客戶(?:是|為|[:：])?|'
    r'旅客(?:是|為|[:：])?|乘客(?:是|為|[:：])?|承辦人(?:是|為|[:：])?|'
    r'負責人(?:是|為|[:：])?|寄件者[:：]|收件者[:：]|'
    r'專員|經理|教授|主管|護理師|醫師|簽名[:：]|署名[:：])\s*$'
)


SUFFIX = re.compile(r'^\s*(?:敬上|謹上|先生|小姐|醫師|教授|主任|專員|經理|里長|店長|您好|你好)(?=您好|你好|$|[\s，。！!、:：])')


BARE_SIGNOFF = re.compile(r'^\s*(?:[\w]{0,10}\s*)?(?:敬上|謹上)\s*$', re.UNICODE)


NOT_NAME_SUFFIX = ('公司','企業','銀行','學校','醫院','商店','協會','大學','集團','基金會','有限公司')


def _han_name(name: str) -> bool:
    if not 2 <= len(name) <= 4 or not all('\u4e00' <= c <= '\u9fff' for c in name):
        return False
    if name.endswith(NOT_NAME_SUFFIX):
        return False
    return name[0] in SURNAMES or any(name.startswith(s) for s in COMPOUND_SURNAMES)


def normalize_spans(text: str, spans: Iterable[tuple[int,int]]) -> set[tuple[int,int]]:
    result = set()
    for a,b in spans:
        a,b = int(a),int(b)
        if not 0 <= a < b <= len(text):
            raise ValueError(f'Invalid span [{a},{b}) for text length {len(text)}')
        result.add((a,b))
    return result


def explicit_context(text: str, a: int, b: int) -> bool:
    # Use only local context, not a distant keyword somewhere in a long document.
    left = text[max(0,a-16):a]
    right = text[b:min(len(text),b+12)]
    if PREFIX.search(left) or SUFFIX.match(right):
        return True
    # Formal sign-off: a whole line NAME followed by 敬上 or 謹上.
    line_start=text.rfind('\n',0,a)+1
    line_end=text.find('\n',b)
    if line_end<0: line_end=len(text)
    line=text[line_start:line_end]
    if line.strip().endswith(('敬上','謹上')) and a>=line_start and b<=line_end:
        remainder = text[b:line_end].strip()
        preceding = text[line_start:a].strip()
        if remainder in ('敬上','謹上') and preceding in ('','客戶','專員','旅客','醫師','申請人','承辦人','聯絡人'):
            return True
    return False


def propagate_repeated_names(text: str, spans: Iterable[tuple[int,int]]):
    """Return (new_spans, newly_added_spans) using only existing predictions.

    A candidate must be an entire 2-4-character surname-led Han span already
    predicted by the baseline, and the new occurrence must have strong local
    person-name context. No benchmark labels, no arbitrary gold name dictionary.
    """
    current = normalize_spans(text, spans)
    seeds = set()
    for s,e in current:
        name = text[s:e]
        if not _han_name(name):
            continue
        if text.count(name)<2: # This patch is ONLY for repeated mentions.
            continue
        # A precise, existing model prediction supplies positive evidence;
        # where possible, require a PERSON context on at least one mention.
        if not explicit_context(text,s,e):
            continue
        seeds.add(name)
    added = set()
    for name in sorted(seeds,key=lambda v:(-len(v),v)):
        start=0
        while True:
            i=text.find(name,start)
            if i<0: break
            j=i+len(name)
            start=i+1
            if (i,j) in current or any(s<=i and e>=j for s,e in current):
                continue
            # No substring of a longer Chinese word/name.
            if i>0 and '\u4e00'<=text[i-1]<='\u9fff' and not explicit_context(text,i,j):
                continue
            if j<len(text) and '\u4e00'<=text[j]<='\u9fff' and not explicit_context(text,i,j):
                continue
            if not explicit_context(text,i,j):
                continue
            overlaps = {(s,e) for s,e in current|added if i<e and j>s}
            # If the baseline detected only a suffix/prefix at this exact
            # context, replace it with the confirmed complete name.  Never
            # discard a different candidate extending beyond the name.
            if overlaps and not all(i<=s and e<=j for s,e in overlaps):
                continue
            current.difference_update(overlaps)
            added.difference_update(overlaps)
            added.add((i,j))
    return current|added,added


@dataclass(frozen=True)
class TokenWindow:
    token_start: int
    token_end: int
    char_start: int
    char_end: int


def _token_offsets(text: str, tokenizer: Any) -> list[tuple[int, int]]:
    encoded = tokenizer(
        text,
        add_special_tokens=False,
        truncation=False,
        return_offsets_mapping=True,
    )
    offsets = encoded.get('offset_mapping')
    if offsets is None:
        raise RuntimeError('Tokenizer 沒有回傳 offset_mapping。')

    clean = []
    previous_start = -1
    for item in offsets:
        s, e = map(int, item)
        if s == e:
            continue
        if not (0 <= s < e <= len(text)) or s < previous_start:
            raise ValueError(f'Invalid tokenizer offset [{s}:{e}] in {len(text)} characters')
        previous_start = s
        clean.append((s, e))
    return clean


def _model_limit(tokenizer: Any) -> int:
    limit = int(getattr(tokenizer, 'model_max_length', 512))
    if limit < 4 or limit > 100_000:
        # Transformers may encode unknown limits as 1e30.
        limit = 512
    return limit


def make_token_windows(
    text: str,
    *,
    tokenizer: Any,
    chunk_tokens: int = 400,
    overlap_tokens: int = 40,
    max_input_tokens: int | None = None,
) -> list[TokenWindow]:
    """Partition text with overlapping token windows and original char offsets.

    A single short input is returned as one full-text window (no needless
    conversion/normalization of normal model outputs). Longer inputs are
    split into <=chunk_tokens content-token windows.
    """
    if not text:
        return []
    if chunk_tokens < 16 or overlap_tokens < 1 or overlap_tokens >= chunk_tokens:
        raise ValueError('需滿足 chunk_tokens>=16 且 0<overlap_tokens<chunk_tokens')

    max_len = max_input_tokens or _model_limit(tokenizer)
    special = int(tokenizer.num_special_tokens_to_add(pair=False))
    if chunk_tokens + special > max_len:
        raise ValueError(
            f'chunk_tokens={chunk_tokens} 超出模型上限 {max_len}（含 {special} 個特殊 Token）。'
        )

    offsets = _token_offsets(text, tokenizer)
    if not offsets:
        return [TokenWindow(0, 0, 0, len(text))]
    n = len(offsets)

    if n + special <= max_len:
        return [TokenWindow(0, n, 0, len(text))]

    output = []
    start = 0
    while start < n:
        end = min(n, start + chunk_tokens)
        char_start = offsets[start][0]
        char_end = offsets[end - 1][1]
        if char_start >= char_end:
            raise ValueError('空的分塊或錯誤的 Token offset。')
        # (char_start, char_end) index original text; never tokenize a
        # normalized version and then use its offsets on original text.
        output.append(TokenWindow(start, end, char_start, char_end))
        if end == n:
            break
        start = end - overlap_tokens
    return output


def _resolve_person_conflicts(results: list[dict]) -> list[dict]:
    """Deduplicate exact spans and prefer full-length overlapping PER spans.

    A name spanning an overlap can appear in both windows. Do not silently
    concatenate text from separate predictions; select one existing span.
    """
    unique: dict[tuple[int, int, str], dict] = {}
    for row in results:
        key = (int(row['start']), int(row['end']), str(row.get('entity_group', '')))
        if key not in unique or float(row.get('score', 0)) > float(unique[key].get('score', 0)):
            unique[key] = row

    candidates = list(unique.values())
    persons = [r for r in candidates if str(r.get('entity_group', '')).upper() in ('PER', 'PERSON')]
    others = [r for r in candidates if str(r.get('entity_group', '')).upper() not in ('PER', 'PERSON')]
    # Full span preferred over a boundary fragment; confidence breaks ties.
    persons.sort(key=lambda r: (-(r['end'] - r['start']), -float(r.get('score', 0)), r['start']))
    chosen: list[dict] = []
    for row in persons:
        if not any(row['start'] < k['end'] and row['end'] > k['start'] for k in chosen):
            chosen.append(row)
    return sorted(chosen + others, key=lambda r: (r['start'], r['end'], str(r.get('entity_group', ''))))


def third_ner_chunked(
    text: str,
    *,
    chunk_tokens: int = 400,
    overlap_tokens: int = 40,
    tokenizer: Any = None,
    predictor: Callable[[str], list[Mapping]] | None = None,
    debug: bool = False,
) -> list[dict]:
    """Drop-in C replacement returning HF entity dictionaries with global spans.

    predictor defaults to ORIGINAL handler_text_v3.third_ner and returns its
    normal raw dict results. Setting tokenizer/predictor lets tests run without
    importing any models.

    Raises on invalid offsets rather than silently losing a privacy span.
    """
    if not isinstance(text, str):
        raise TypeError('text must be str')
    if not text:
        return []
    if tokenizer is None:
        tokenizer = _get_c_tokenizer()
    if predictor is None:
        predictor = third_ner

    windows = make_token_windows(
        text, tokenizer=tokenizer,
        chunk_tokens=chunk_tokens,
        overlap_tokens=overlap_tokens,
    )

    # SHORT text: do not change baseline output or force dedup.
    if len(windows) == 1 and windows[0].char_start == 0 and windows[0].char_end == len(text):
        return list(predictor(text))

    raw = []
    for i, win in enumerate(windows):
        part = text[win.char_start:win.char_end]
        # Defensive check: if the tokenizer's normalization makes the
        # independently encoded chunk unexpectedly exceed the limit, fail
        # rather than permit silent truncation by the predictor.
        encoded = tokenizer(part, add_special_tokens=True, truncation=False)
        if len(encoded['input_ids']) > _model_limit(tokenizer):
            raise ValueError(f'分塊 {i} 的 token 數超過模型限制。')
        if debug:
            print(f'[C-CHUNK] {i+1}/{len(windows)} '
                  f'tokens={win.token_start}:{win.token_end} '
                  f'chars={win.char_start}:{win.char_end} '
                  f'length={len(encoded["input_ids"])}')
        rows = predictor(part)
        for result in rows:
            if not isinstance(result, Mapping) or 'start' not in result or 'end' not in result:
                raise TypeError(f'C third_ner 結果應是含 start/end 的 dict：{result!r}')
            s, e = int(result['start']), int(result['end'])
            if not 0 <= s < e <= len(part):
                raise ValueError(f'分塊 {i} 的預測座標無效：[{s}:{e}]')
            global_s, global_e = win.char_start+s, win.char_start+e
            row = dict(result)
            row['start'] = global_s
            row['end'] = global_e
            # Make the displayed word human-readable and prevent mismatch
            # between source text and HF model's subword-marked 'word'.
            # Keep the original 'word' if consumers rely on HF aggregation.
            raw.append(row)

    return _resolve_person_conflicts(raw)


def _get_c_tokenizer():
    # 與 C Pipeline 共用同一個 tokenizer，不多載入一份。
    _get_c_pipeline()
    return _c_tokenizer


def _dedup_results(rows):
    merged = {}
    for row in rows:
        s, e, label, score, source = row[:5]
        key = (int(s), int(e), label)
        if key not in merged or float(score or 0) > float(merged[key][3] or 0):
            merged[key] = tuple(row[:5])
    return list(merged.values())


def _postprocess_a(text: str, clu: list[tuple]) -> list[tuple]:
    """與 compare_three_ner_fixed.postprocess_base 的 A-only 分支同序。"""
    results = expand_compound_surname(text, list(clu))
    results = _dedup_results(results)
    results = resolve_overlapping_person_spans(results)
    results = correct_person_title_boundary(text, results)
    results = filter_person_in_organization_context(text, results)
    results = detect_surname_with_title(text, results)
    results = filter_business_person_false_positives(text, results)
    return _dedup_results(results)


def _repeat_rows(text: str, person_rows: list[tuple]) -> list[tuple]:
    """保留預測分數及來源，只將同篇文件已確認的完整姓名補齊。"""
    original = [tuple(r[:5]) for r in person_rows
                if r[2] == 'PERSON' and 0 <= int(r[0]) < int(r[1]) <= len(text)]
    positions = {(int(r[0]), int(r[1])) for r in original}
    patched, _ = propagate_repeated_names(text, positions)
    seed = {}
    for s, e, _, score, source in original:
        name = text[s:e]
        if score is not None and (name not in seed or float(score) > float(seed[name][0])):
            seed[name] = (score, source)
    out = {}
    for row in original:
        key = row[:2]
        if key in patched and (key not in out or float(row[3] or 0) > float(out[key][3] or 0)):
            out[key] = row
    for s, e in patched - positions:
        if text[s:e] not in seed:
            raise RuntimeError("Repeat 找不到已辨識的人名種子")
        score, _ = seed[text[s:e]]
        out[(s, e)] = (s, e, 'PERSON', score, 'zh')
    return sorted(out.values(), key=lambda r: (r[0], r[1]))



# ---------- Production adapter: teammate's A inference is injected from handler_text.py ----------
_C_WARNING_EMITTED = False

def ensemble_from_a(text: str, a_rows: Sequence[tuple]) -> list[tuple]:
    """Preserve original teammate CLUENER inference; combine A+C+Repeat with C-only chunking.

    a_rows must be 5-tuples with Python source-text offsets, as emitted by the
    team's original handler_text.zh_ner(). Output uses the same 5-tuple API.
    """
    if not text:
        return []
    global _C_WARNING_EMITTED
    base = _postprocess_a(text, list(a_rows))
    try:
        if C_LONG_TEXT_MODE == 'chunk':
            c_raw = third_ner_chunked(text, chunk_tokens=C_CHUNK_TOKENS, overlap_tokens=C_OVERLAP_TOKENS)
        else:
            c_raw = third_ner(text)
    except (OSError, ImportError) as exc:
        # Missing local model should not disable A or the team's Regex/Presidio.
        # Do NOT ignore bad token offsets or other integration errors.
        if not _C_WARNING_EMITTED:
            print(f'[警告] C 模型無法載入，暫時僅使用 A+Repeat；請確認離線模型：{exc}')
            _C_WARNING_EMITTED = True
        c_raw = []
    fused = merge_v32(text, base, c_raw, ckip_raw=None)
    persons = _repeat_rows(text, [r for r in fused if str(r[2]).upper() == 'PERSON'])
    others = [r for r in fused if str(r[2]).upper() != 'PERSON']
    dedup = _dedup_results(others + persons)
    # Crucial: core.merge_overlaps understands only regex/en/zh source tiers.
    # Source values such as COMPOUND_SURNAME_* must map to zh.
    normalized = []
    for s, e, label, score, source in dedup:
        s, e = int(s), int(e)
        if not (0 <= s < e <= len(text)):
            raise ValueError(f'NER 座標超出原文範圍: [{s}:{e}] of {len(text)}')
        if source not in {'regex', 'en', 'zh'}:
            source = 'zh'
        normalized.append((s, e, label, score, source))
    return sorted(normalized, key=lambda r: (r[0], r[1], r[2]))
