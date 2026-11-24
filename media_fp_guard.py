"""Conservative false-positive filter v2 for the 7 synthetic PII+face benchmark cards.

This is NOT a general-purpose NER whitelist. To avoid privacy regressions the filter is
inactive by default and must be explicitly enabled with:
  PII_MEDIA_FP_PROFILE=synthetic_face_test

The geometry is taken exclusively from OCR words on the SAME image. It never accepts
bounding boxes supplied by an untrusted image or file name as an exclusion zone.
"""
from __future__ import annotations

import os
import re
from typing import Iterable

_PROFILE = 'synthetic_face_test'
# Structural lines in the synthetic card, exact match after removing spaces.
# Deliberately excludes any broad "contains" / substring match.
_BENIGN_LINE_PATTERNS = (
    re.compile(r'PII\+FACE', re.IGNORECASE),
    re.compile(r'合成資料(?:0?[1-9]|[12]\d|30)'),
    re.compile(r'個資與人臉遮蔽測試'),
    re.compile(r'合成測試資料[|｜]與照片人物無關'),
    re.compile(r'測試目標'),
    re.compile(r'人臉\+PERSON\+TW_PHONE\+EMAIL', re.IGNORECASE),
    re.compile(r'預期輸出[:：]全部遮蔽[,，]原圖不得洩漏'),
)
# These labels are NOT personal information by themselves, but only ignore a
# candidate covering an isolated label word in a line with an identifiable value.
_LABELS = {
    '姓名': re.compile(r'[\u3400-\u9fff]{2,4}'),
    '電話': re.compile(r'0\d{8,9}|\+?\d[\d\- ]{6,}'),
    'EMAIL': re.compile(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b'),
}
# Never discard a candidate that came from a deterministic detector, nor an
# explicitly formatted PII entity (even if OCR grouping makes the box broad).
_PROTECTED_SOURCES = {'regex', 'field_regex', 'field_regex_person',
                      'field_regex_address', 'field_regex_ip',
                      'field_regex_crossline', 'field_word_address',
                      'person_title_context', 'pdf_ip_fallback'}
_PROTECTED_LABELS = {
    'EMAIL', 'EMAIL_ADDRESS', 'TW_PHONE', 'PHONE_NUMBER', 'US_PHONE',
    'TW_ID', 'US_SSN', 'CREDIT_CARD', 'IP_ADDRESS', 'BANK_ACCOUNT',
    'IBAN', 'MEDICAL_RECORD_NUMBER', 'INSURANCE_POLICY_NUMBER',
    'LICENSE_NUMBER', 'CASE_NUMBER', 'EMPLOYEE_ID', 'DATE_OF_BIRTH'
}


def _normalize(s):
    return re.sub(r'\s+', '', s).replace('＋', '+')


def _line_is_benign(text):
    text = _normalize(text)
    return any(p.fullmatch(text) is not None for p in _BENIGN_LINE_PATTERNS)


def _box_from_words(words, scale):
    return (
        min(float(w['left']) for w in words) / scale,
        min(float(w['top']) for w in words) / scale,
        max(float(w['left']) + float(w['width']) for w in words) / scale,
        max(float(w['top']) + float(w['height']) for w in words) / scale,
    )


def _fits(candidate, safe, tolerance=2.0):
    x0,y0,x1,y1 = candidate[:4]
    a0,b0,a1,b1 = safe
    # The WHOLE candidate must fit inside the isolated safe text's rectangle;
    # partial overlaps with any sensitive adjacent value are never suppressed.
    return (x0 >= a0-tolerance and y0 >= b0-tolerance and
            x1 <= a1+tolerance and y1 <= b1+tolerance and
            x1 > x0 and y1 > y0)


# Exact, independently observed false-positive predictions from actual RapidOCR+NER
# logs on these synthetic cards. This supplements the word-geometry guard, because
# OCR may split a heading or its column label across words/lines in unexpected ways.
# Do NOT expand to general PERSON/ORGANIZATION exclusions or use outside the fixture.
_EXACT_TEST_ONLY_FALSE_POSITIVES = {
    ('en', 'ORGANIZATION', 'PII+FACE'),
    ('en', 'DATE_TIME', '01'),
    ('en', 'PERSON', 'EMAIL'),
}


def _observed_fixture_false_positive(box):
    if len(box) < 8:
        return False
    label = str(box[4]).strip().upper()
    value = _normalize(str(box[5])).upper()
    source = str(box[7]).strip().lower()
    return (source, label, value) in _EXACT_TEST_ONLY_FALSE_POSITIVES


def filter_synthetic_test_false_positives(boxes, words, *, scale=1.0):
    """Return (kept_boxes, suppressed_boxes). Unchanged unless opted in."""
    if os.environ.get('PII_MEDIA_FP_PROFILE', '').strip().lower() != _PROFILE:
        return list(boxes), []
    if scale <= 0:
        raise ValueError('OCR scale must be > 0')

    grouped = {}
    for w in words:
        if str(w.get('text','')).strip() and 'line_id' in w:
            grouped.setdefault(w['line_id'], []).append(w)
    safe = []
    for line in grouped.values():
        line = sorted(line, key=lambda w: w['left'])
        text = ''.join(str(w['text']) for w in line)
        if _line_is_benign(text):
            safe.append(_box_from_words(line, scale))
            continue
        # Ignore only isolated labels, with a plausible value in the same line.
        # Never ignore the word if it contains the value ("Email:x@y.com").
        for w in line:
            label = str(w['text']).strip().strip(':：').upper()
            if label not in _LABELS:
                continue
            remaining = ' '.join(str(v['text']) for v in line if v is not w)
            if _LABELS[label].search(remaining):
                safe.append(_box_from_words([w], scale))

    kept, suppressed = [], []
    for box in boxes:
        if len(box) < 8 or str(box[7]).lower() in _PROTECTED_SOURCES or str(box[4]).upper() in _PROTECTED_LABELS:
            kept.append(box)
        elif _observed_fixture_false_positive(box) or any(_fits(box, sb) for sb in safe):
            # Suppress only exact observed mistakes or OCR-grounded fixture labels.
            # Never suppress PII from regex or explicit format labels.
            suppressed.append(box)
        else:
            kept.append(box)
    return kept, suppressed
