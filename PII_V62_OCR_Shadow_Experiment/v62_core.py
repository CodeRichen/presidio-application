"""V6.2 synthetic OCR research helpers. No production code changes."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class Features:
    words: int
    min_word_height_pt: float
    primary_pii_boxes: int
    contrast_std: float
    edge_mean: float


def decide(f: Features) -> tuple[bool, list[str]]:
    """EXPERIMENT ONLY. True = would skip 3x; not a safe deployment policy.

    A 2x OCR hit can never prove that all other PII was found.
    This heuristic MUST NOT be used to redact real documents.
    """
    reasons = []
    if f.words < 9:
        reasons.append('few_2x_words')
    if f.min_word_height_pt < 10.5:
        reasons.append('small_or_missing_2x_words')
    if f.primary_pii_boxes < 2:
        reasons.append('few_2x_pii_boxes')
    if f.contrast_std < 15:
        reasons.append('low_contrast')
    if f.edge_mean < 6:
        reasons.append('low_edge_strength')
    return (not reasons), (reasons or ['meets_experimental_quality_heuristic'])


def union_area(rects: list[tuple[float, float, float, float]], bounds) -> float:
    """Exact union area of clipped axis-aligned rectangles (no image needed)."""
    x0, y0, x1, y1 = map(float, bounds)
    clipped=[]
    for a,b,c,d in rects:
        aa,bb,cc,dd = max(x0,a),max(y0,b),min(x1,c),min(y1,d)
        if cc>aa and dd>bb:
            clipped.append((aa,bb,cc,dd))
    if not clipped:
        return 0.0
    xs=sorted({v for a,b,c,d in clipped for v in (a,c)})
    area=0.0
    for left,right in zip(xs,xs[1:]):
        intervals=sorted((b,d) for a,b,c,d in clipped if a < right and c > left)
        if not intervals:
            continue
        length=0.0
        lo,hi=intervals[0]
        for a,b in intervals[1:]:
            if a>hi:
                length+=hi-lo
                lo,hi=a,b
            else:
                hi=max(hi,b)
        length+=hi-lo
        area+=(right-left)*length
    return area


def coverage(gt_rect, boxes, label: str) -> float:
    x0,y0,x1,y1=gt_rect
    a=max(0,(x1-x0)*(y1-y0))
    if a<=0:
        return 0.0
    same=[tuple(map(float,b[:4])) for b in boxes if str(b[4])==label]
    return min(1.,union_area(same,gt_rect)/a)


def audit_gt(gt: list[dict], boxes, threshold: float=0.95) -> dict:
    records=[{'label':r['label'], 'coverage':round(coverage(r['bbox_pdf'],boxes,r['label']),4)} for r in gt]
    bad=[{'label':r['label'], 'index':i+1, 'coverage':r['coverage']} for i,r in enumerate(records) if r['coverage']<threshold]
    return {'expected':len(gt), 'pass_count':len(gt)-len(bad), 'failed':bad}


def metrics_from_words(words: list[dict], zoom: float) -> tuple[int,float]:
    heights=[float(w.get('height',0))/zoom for w in words if float(w.get('height',0))>0]
    return len(words), round(min(heights),3) if heights else 0.0
