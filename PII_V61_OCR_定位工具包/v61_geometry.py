# -*- coding: utf-8 -*-
"""Pure geometry helpers. No OCR, model imports, or PII content."""
from __future__ import annotations
import math


def rect_area(box):
    x0, y0, x1, y1 = (float(v) for v in box[:4])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def intersection_area(a, b):
    x0, y0 = max(float(a[0]), float(b[0])), max(float(a[1]), float(b[1]))
    x1, y1 = min(float(a[2]), float(b[2])), min(float(a[3]), float(b[3]))
    return max(0.0, x1-x0) * max(0.0, y1-y0)


def overlap_fraction_of_smaller(a, b):
    small = min(rect_area(a), rect_area(b))
    return intersection_area(a,b)/small if small else 0.0


def normalized_box(box, width, height):
    if width <= 0 or height <= 0:
        raise ValueError('invalid page dimensions')
    items = [float(box[0])/width*100, float(box[1])/height*100,
             float(box[2])/width*100, float(box[3])/height*100]
    if not all(math.isfinite(x) for x in items):
        raise ValueError('nonfinite bounding box')
    return [round(min(100.0,max(0.0,x)),2) for x in items]


def expected_rect_to_pdf(box_px, ref_width, ref_height, page_width, page_height):
    if ref_width <= 0 or ref_height <= 0:
        raise ValueError('invalid reference dimensions')
    return (box_px[0]/ref_width*page_width,
            box_px[1]/ref_height*page_height,
            box_px[2]/ref_width*page_width,
            box_px[3]/ref_height*page_height)


def safe_category(text, allowed, fallback='OTHER'):
    # Explicit whitelist: never serialize model text, OCR tokens or arbitrary source strings.
    value = str(text)
    return value if value in allowed else fallback


LABELS = frozenset({
    'PERSON','PERSON_TITLE','TW_PHONE','PHONE_NUMBER','US_PHONE','TW_ID',
    'EMAIL','EMAIL_ADDRESS','IP_ADDRESS','ADDRESS','ORGANIZATION','DATE_TIME',
    'DATE_OF_BIRTH','CREDIT_CARD','BANK_ACCOUNT','BANK_NAME','CASE_NUMBER',
    'LICENSE_NUMBER','MEDICAL_RECORD_NUMBER','INSURANCE_POLICY_NUMBER',
    'EMPLOYEE_ID','CREDENTIAL_LIKE','URL','FILE_NAME','GENERIC_SECRET',
    'US_SSN','POSITION',
})
SOURCES = frozenset({
    'regex','presidio','cluener','ckip','spacy','ner','zh_ner', 'en_ner',
    'field_regex','field_regex_crossline','field_regex_person','field_regex_ip',
    'field_regex_address','field_word_address','pdf_field_value',
    'pdf_ip_fallback','person_title_context',
})


def describe_added_boxes(primary, added, sample, page_width, page_height):
    """Returns metadata only; box[5] original value is never accessed."""
    rw,rh = sample['size_px']
    known = []
    for entry in sample['expected_text']:
        known.append((safe_category(entry['label'], LABELS),
                      expected_rect_to_pdf(entry['bbox_px'],rw,rh,page_width,page_height)))
    out=[]
    for b in added:
        rect=tuple(float(z) for z in b[:4])
        if rect_area(rect)==0 or not all(math.isfinite(z) for z in rect):
            raise ValueError('invalid OCR box')
        overlaps=[(intersection_area(rect, gt), label, overlap_fraction_of_smaller(rect,gt)) for label,gt in known]
        closest=max(overlaps,default=(0.0,'NONE',0.0),key=lambda x:x[0])
        primary_overlap=max((overlap_fraction_of_smaller(rect,p[:4]) for p in primary),default=0.0)
        raw_score=b[6] if len(b)>6 else None
        score=(round(float(raw_score),3)
               if raw_score is not None and math.isfinite(float(raw_score)) else None)
        record={
            'page':6,
            'label':safe_category(b[4],LABELS),
            'source_type':safe_category(b[7] if len(b)>7 else '',SOURCES),
            'bbox_percent_xyxy':normalized_box(rect,page_width,page_height),
            'score':score,
            'overlap_known_text_label':closest[1] if closest[0]>0 else None,
            'overlap_known_text_fraction_of_smaller':round(closest[2],3),
            'max_overlap_with_any_2x_box':round(primary_overlap,3),
            'review':'MANUAL_REVIEW_REQUIRED',
        }
        out.append(record)
    return out
