"""離線 A+C+Repeat 人名偵測引擎（無詞表版）。

    detect(text, a_rows=())         -> [(start, end, label, score, source), ...]
    warmup()                        -> 預載 C 模型，回傳是否可用
    c_contribution(text, a_rows=()) -> 僅列出 C / Repeat 新增或修正的結果（除錯用）

環境變數（皆可省略）：
    PII_C_LONG_MODE      chunk | baseline（預設 chunk）
    PII_C_CHUNK_TOKENS / PII_C_OVERLAP_TOKENS   預設 400 / 40
    PII_C_MIN_SCORE      C 採用門檻（預設 0.80）
    PII_C_LABELS         C 標籤對應，如 "PER:PERSON,ORG:ORGANIZATION"（預設 PER:PERSON）
    PII_C_MAX_EXTEND     C 可延伸 A 既有 span 的最大字數（預設 4）
    PII_REPEAT_MIN_LEN   重複人名補齊的最短字數（預設 3）
不含任何寫死字表；商號誤判改由 A 自己輸出的 ORGANIZATION 結果判斷。
"""
from __future__ import annotations

import logging
import os
import unicodedata
from functools import lru_cache
from typing import Sequence

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

__all__ = ["detect", "ensemble_from_a", "warmup", "c_contribution"]
_log = logging.getLogger(__name__)


def _env(name, default, cast=str):
    try:
        return cast(os.environ.get(name, default))
    except ValueError:
        _log.warning("%s 格式錯誤，改用預設值 %s", name, default)
        return cast(default)


C_LONG_TEXT_MODE = _env("PII_C_LONG_MODE", "chunk").strip().lower()
if C_LONG_TEXT_MODE not in ("baseline", "chunk"):
    _log.warning("PII_C_LONG_MODE 必須是 baseline 或 chunk，改用 chunk")
    C_LONG_TEXT_MODE = "chunk"
C_CHUNK_TOKENS = _env("PII_C_CHUNK_TOKENS", 400, int)
C_OVERLAP_TOKENS = _env("PII_C_OVERLAP_TOKENS", 40, int)
C_MIN_SCORE = _env("PII_C_MIN_SCORE", 0.80, float)
C_MAX_EXTEND = _env("PII_C_MAX_EXTEND", 4, int)
REPEAT_MIN_LEN = _env("PII_REPEAT_MIN_LEN", 3, int)
C_MODEL_ID = "Davlan/bert-base-multilingual-cased-ner-hrl"
C_LABELS = dict(p.split(":", 1) for p in _env("PII_C_LABELS", "PER:PERSON").split(",") if ":" in p)
_ORG = ("ORG", "ORGANIZATION", "COMPANY")


def _is_han(ch: str) -> bool:
    return '\u4e00' <= ch <= '\u9fff'


# ---------------- C 模型 ----------------
@lru_cache(maxsize=1)
def _get_c_pipeline():
    from transformers import pipeline, AutoTokenizer, AutoModelForTokenClassification
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
    for cand in candidates:
        try:
            tok = AutoTokenizer.from_pretrained(cand, local_files_only=True, use_fast=True)
            model = AutoModelForTokenClassification.from_pretrained(cand, local_files_only=True)
            break
        except (OSError, ValueError) as exc:
            errors.append(f'{cand}: {exc}')
    else:
        raise OSError('C 模型快取不完整：' + '; '.join(errors))
    return pipeline("ner", model=model, tokenizer=tok, aggregation_strategy="simple")


def third_ner(text: str) -> list[dict]:
    return list(_get_c_pipeline()(text)) if text else []


_c_error: Exception | None = None


def warmup() -> bool:
    """預載 C 模型；載入失敗只警告一次並記住。"""
    global _c_error
    if _c_error is None:
        try:
            _get_c_pipeline()
        except (OSError, ImportError) as exc:
            _c_error = exc
            _log.warning("C 模型無法載入，僅使用 A：%s", exc)
    return _c_error is None


def _model_limit(tok) -> int:
    limit = int(getattr(tok, 'model_max_length', 512))
    return 512 if limit < 4 or limit > 100_000 else limit


def make_token_windows(text, tok, chunk_tokens=C_CHUNK_TOKENS, overlap_tokens=C_OVERLAP_TOKENS):
    """回傳原文字元座標 (char_start, char_end) 重疊視窗；短文本回傳單一全文視窗。"""
    if not text:
        return []
    if chunk_tokens < 16 or not 0 < overlap_tokens < chunk_tokens:
        raise ValueError('需滿足 chunk_tokens>=16 且 0<overlap_tokens<chunk_tokens')
    special, limit = int(tok.num_special_tokens_to_add(pair=False)), _model_limit(tok)
    if chunk_tokens + special > limit:
        raise ValueError(f'chunk_tokens={chunk_tokens} 超出模型上限 {limit}（含 {special} 個特殊 Token）')
    enc = tok(text, add_special_tokens=False, truncation=False, return_offsets_mapping=True)
    if enc.get('offset_mapping') is None:
        raise RuntimeError('Tokenizer 沒有回傳 offset_mapping（需使用 fast tokenizer）。')
    offsets, prev = [], -1
    for s, e in (tuple(map(int, x)) for x in enc['offset_mapping']):
        if s == e:
            continue
        if not (0 <= s < e <= len(text)) or s < prev:
            raise ValueError(f'Invalid tokenizer offset [{s}:{e}] in {len(text)} characters')
        prev = s
        offsets.append((s, e))
    n = len(offsets)
    if n + special <= limit:
        return [(0, len(text))]
    windows, start = [], 0
    while start < n:
        end = min(n, start + chunk_tokens)
        windows.append((offsets[start][0], offsets[end - 1][1]))
        if end == n:
            break
        start = end - overlap_tokens
    return windows


def _resolve_c_conflicts(rows):
    """同 span 取高分；同標籤重疊取最長（同長取高分）。"""
    unique = {}
    for r in rows:
        key = (int(r['start']), int(r['end']), str(r.get('entity_group', '')).upper())
        if key not in unique or float(r.get('score', 0)) > float(unique[key].get('score', 0)):
            unique[key] = r
    chosen = []
    for r in sorted(unique.values(), key=lambda r: (-(r['end'] - r['start']), -float(r.get('score', 0)), r['start'])):
        g = str(r.get('entity_group', '')).upper()
        if not any(g == str(k.get('entity_group', '')).upper() and r['start'] < k['end'] and r['end'] > k['start'] for k in chosen):
            chosen.append(r)
    return sorted(chosen, key=lambda r: (r['start'], r['end']))


def third_ner_chunked(text, chunk_tokens=C_CHUNK_TOKENS, overlap_tokens=C_OVERLAP_TOKENS):
    """C 分塊版：回傳全域座標的 HF entity dict；座標或長度異常直接報錯。"""
    if not text:
        return []
    tok = _get_c_pipeline().tokenizer
    windows = make_token_windows(text, tok, chunk_tokens, overlap_tokens)
    if len(windows) == 1:
        return third_ner(text)
    raw = []
    for i, (cs, ce) in enumerate(windows):
        part = text[cs:ce]
        if len(tok(part, add_special_tokens=True, truncation=False)['input_ids']) > _model_limit(tok):
            raise ValueError(f'分塊 {i} 的 token 數超過模型限制。')
        for r in third_ner(part):
            s, e = int(r['start']), int(r['end'])
            if not 0 <= s < e <= len(part):
                raise ValueError(f'分塊 {i} 的預測座標無效：[{s}:{e}]')
            raw.append({**r, 'start': cs + s, 'end': cs + e})
    return _resolve_c_conflicts(raw)


def _run_c(text: str) -> list[dict]:
    if not warmup():
        return []
    return third_ner_chunked(text) if C_LONG_TEXT_MODE == 'chunk' else third_ner(text)


# ---------------- 整併 ----------------
def _dedup(rows):
    merged = {}
    for r in rows:
        key = (int(r[0]), int(r[1]), r[2])
        if key not in merged or float(r[3] or 0) > float(merged[key][3] or 0):
            merged[key] = tuple(r[:5])
    return list(merged.values())


def _clean(rows):
    """去重；被較長 PERSON 完全包含的 PERSON 移除；PERSON 與 A 的 ORG 重疊或緊鄰其前方時視為商號移除。"""
    orgs = [r for r in rows if r[2] in _ORG]
    out = []
    for c in sorted(_dedup(rows), key=lambda m: (-(m[1] - m[0]), -(m[3] or 0), m[0])):
        if c[2] == "PERSON":
            if any(x[2] == "PERSON" and x[0] <= c[0] and c[1] <= x[1] for x in out):
                continue
            if any((c[0] < o[1] and c[1] > o[0]) or o[0] == c[1] for o in orgs):
                continue
        out.append(c)
    return out


def _trim(text, s, e):
    while s < e and text[s].isspace():
        s += 1
    while e > s and text[e - 1].isspace():
        e -= 1
    return s, e


def _extension_ok(text, lo, hi, s, e):
    """C 延伸 A 既有 span：新增字元限字母/數字，且字數受限。"""
    extra = text[s:lo] + text[hi:e]
    return len(extra) <= C_MAX_EXTEND and all(unicodedata.category(ch)[0] in "LN" for ch in extra)


def _c_candidates(text, c_raw):
    for item in sorted(c_raw, key=lambda x: (int(x["start"]), int(x["end"]))):
        label = C_LABELS.get(str(item.get("entity_group", "")).upper())
        score = float(item.get("score", 0))
        if label is None or score < C_MIN_SCORE:
            continue
        s, e = _trim(text, int(item["start"]), int(item["end"]))
        if 0 <= s < e <= len(text) and 2 <= e - s <= 20 and "\n" not in text[s:e]:
            yield s, e, label, score


def merge_c(text, base, c_raw):
    """C 結果：與 A 無重疊 -> 新增；A 同標籤片段被 C 完整包含且延伸合理 -> 取代；A 判為其他類型 -> 不覆蓋。"""
    rows = list(base)
    for s, e, label, score in _c_candidates(text, c_raw):
        hit = [r for r in rows if s < r[1] and e > r[0]]
        if any(r[2] != label for r in hit):
            continue
        new = (s, e, label, score, "zh_c")
        if not hit:
            rows.append(new)
            continue
        lo, hi = min(r[0] for r in hit), max(r[1] for r in hit)
        if (s, e) != (lo, hi) and s <= lo and e >= hi and _extension_ok(text, lo, hi, s, e):
            rows = [r for r in rows if r not in hit]
            rows.append(new)
    return _clean(rows)


def propagate_repeated_names(text, rows):
    """已辨識的 2~4 字漢字姓名，補到全文其他未被任何實體佔用的出現處（僅 >= REPEAT_MIN_LEN 字）。"""
    seeds = {}
    for s, e, label, score, _ in rows:
        name = text[s:e]
        if label == "PERSON" and REPEAT_MIN_LEN <= len(name) <= 4 and all(_is_han(c) for c in name):
            seeds[name] = max(seeds.get(name, 0.0), float(score or 0))
    occupied = [(r[0], r[1]) for r in rows]
    added = []
    for name in sorted(seeds, key=lambda v: (-len(v), v)):
        i = text.find(name)
        while i >= 0:
            j = i + len(name)
            if not any(i < b and j > a for a, b in occupied):
                added.append((i, j, "PERSON", seeds[name], "zh_repeat"))
                occupied.append((i, j))
            i = text.find(name, i + 1)
    return added


def _pipeline(text, a_rows):
    fused = merge_c(text, _clean(list(a_rows)), _run_c(text))
    return fused + propagate_repeated_names(text, fused)


# ---------------- 對外入口 ----------------
def ensemble_from_a(text: str, a_rows: Sequence[tuple] = ()) -> list[tuple]:
    """A（可空）+ C + Repeat；輸出 5-tuple，source 正規化為 regex/en/zh。"""
    if not isinstance(text, str):
        raise TypeError(f'text 必須是 str，收到 {type(text).__name__}')
    if not text:
        return []
    out = []
    for s, e, label, score, src in _dedup(_pipeline(text, a_rows)):
        if not 0 <= s < e <= len(text):
            raise ValueError(f'NER 座標超出原文範圍: [{s}:{e}] of {len(text)}')
        out.append((s, e, label, score, src if src in {'regex', 'en', 'zh'} else 'zh'))
    return sorted(out, key=lambda r: (r[0], r[1], r[2]))


def c_contribution(text: str, a_rows: Sequence[tuple] = ()) -> list[tuple]:
    """除錯：列出 C（zh_c）與 Repeat（zh_repeat）新增或修正的結果。"""
    return [r for r in _pipeline(text, a_rows) if r[4] in ("zh_c", "zh_repeat")]


detect = ensemble_from_a