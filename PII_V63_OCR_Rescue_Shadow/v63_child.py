"""V6.3 child runner: actual 2x/3x OCR. No OCR strings or source pixels in output.

For synthetic-only use.  Parent suppresses child stdout/stderr and keeps inputs
in an ephemeral scratch directory.  This module does not apply redactions.
"""
from __future__ import annotations
import argparse, hashlib, json, os, sys, time
from pathlib import Path
from PIL import ImageStat, ImageFilter
from v63_policy import Features, decide, metrics_from_words, coverage

PASS_THRESHOLD=0.95  # SAME labelled-bbox heuristic used in V6.2; not visual mask audit.

def page_quality(img):
    small=img.copy()
    small.thumbnail((500,500))
    gray=small.convert('L')
    return (round(float(ImageStat.Stat(gray).stddev[0]),3),
            round(float(ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0]),3))

def classified(gt, primary, fallback, merged, would_skip):
    rows=[]
    for i,r in enumerate(gt):
        label=r['label'];bbox=r['bbox_pdf']
        p=coverage(bbox,primary,label)>=PASS_THRESHOLD
        f=coverage(bbox,fallback,label)>=PASS_THRESHOLD
        d=coverage(bbox,merged,label)>=PASS_THRESHOLD
        selected=p if would_skip else d
        rows.append({'idx':i+1,'role':r['role'],'label':label,
                     'zoom2_pass':p,'zoom3_pass':f,'dual_pass':d,
                     'candidate_pass':selected,
                     'rescue_by_3x':bool(d and not p),
                     'dangerous_skip':bool(would_skip and d and not p)})
    return rows

def main(argv=None):
    ap=argparse.ArgumentParser()
    for k in ('root','fixture','ground-truth','sha256','report'): ap.add_argument('--'+k,required=True)
    a=ap.parse_args(argv)
    src=Path(a.fixture)
    h=hashlib.sha256()
    with src.open('rb') as f:
        for piece in iter(lambda:f.read(1<<20),b''): h.update(piece)
    if h.hexdigest()!=a.sha256: raise RuntimeError('fixture checksum changed')
    root=Path(a.root).resolve()
    if not (root/'handler_media.py').is_file(): raise FileNotFoundError('handler_media.py')
    os.environ.update({
        'PII_OCR_BACKEND':'rapid','PII_NER_BACKEND':'ac',
        'PII_MEDIA_FP_REPORT':'0','PII_MEDIA_CACHE':'1',
        'PII_ZH_SINGLE_PASS':'1','PII_FACE_ENABLED':'0',
        'PII_MEDIA_FP_PROFILE':'',
    })
    sys.path.insert(0,str(root))
    import handler_media as hm
    try: import pymupdf as fitz
    except ImportError: import fitz
    gt=json.loads(Path(a.ground_truth).read_text('utf-8'))
    results=[]
    with fitz.open(str(src)) as pdf:
        if len(pdf)!=len(gt) or any(p.rotation for p in pdf):
            raise RuntimeError('unexpected synthetic fixture page geometry')
        for page,meta in zip(pdf,gt):
            obs={'zoom':0.0,'words':0,'min_pt':0.0}
            original=hm.ocr_words
            def spy(img):
                words=original(img)
                if obs['zoom']==2.0:
                    obs['words'],obs['min_pt']=metrics_from_words(words,2.0)
                return words
            hm.ocr_words=spy
            try:
                @hm._pii_media_cache_scope
                def probe():
                    obs['zoom']=2.0
                    start=time.perf_counter()
                    primary,preview=hm._collect_pdf_ocr_boxes(page,2.0)
                    t2=time.perf_counter()-start
                    contrast,edge=page_quality(preview)
                    del preview
                    feat=Features(obs['words'],obs['min_pt'],len(primary),contrast,edge)
                    would_skip,reasons=decide(feat)  # Frozen V6.2 policy; no GT used.
                    obs['zoom']=3.0
                    start=time.perf_counter()
                    fallback,preview3=hm._collect_pdf_ocr_boxes(page,3.0)
                    t3=time.perf_counter()-start
                    del preview3
                    merged=hm._merge_pdf_multizoom_boxes(primary,fallback)
                    return feat,would_skip,reasons,primary,fallback,merged,t2,t3
                features,skip,reasons,primary,fallback,merged,t2,t3=probe()
            finally:
                hm.ocr_words=original
            detail=classified(meta['expected_text'],primary,fallback,merged,skip)
            # Never serialize OCR text / PII value / locations / private map.
            count=lambda key:sum(bool(z[key]) for z in detail)
            probes=[x for x in detail if x['role']=='probe']
            results.append({
                'case':meta['case'],'recipe':meta['recipe'],'probe_font_px':meta['font_px'],
                'gt_count':len(detail),'gt_probe_count':len(probes),
                'zoom2_gt_pass':count('zoom2_pass'),
                'zoom3_gt_pass':count('zoom3_pass'),
                'dual_gt_pass':count('dual_pass'),
                'candidate_gt_pass':count('candidate_pass'),
                'rescues_total':count('rescue_by_3x'),
                'rescues_in_probes':sum(z['rescue_by_3x'] for z in probes),
                'dangerous_skips':count('dangerous_skip'),
                'dual_gt_gaps':len(detail)-count('dual_pass'),
                'would_skip_3x':skip,'decision_reasons':reasons,
                '2x_words':features.words,'min_word_height_pt':features.min_word_height_pt,
                '2x_pii_boxes':len(primary),'merged_pii_boxes':len(merged),
                '3x_extra_boxes':len(merged)-len(primary),
                'contrast_std':features.contrast_std,'edge_mean':features.edge_mean,
                'zoom2_s':round(t2,3),'zoom3_s':round(t3,3),
                'omitted_3x_s_estimate':round(t3 if skip else 0,3),
                'gt_evaluation':[{'role':z['role'],'label':z['label'],
                                   'zoom2_pass':z['zoom2_pass'],
                                   'zoom3_pass':z['zoom3_pass'],
                                   'dual_pass':z['dual_pass'],
                                   'candidate_pass':z['candidate_pass'],
                                   'rescue_by_3x':z['rescue_by_3x'],
                                   'dangerous_skip':z['dangerous_skip']}
                                  for z in detail]
            })
    Path(a.report).write_text(json.dumps({'cases':results},ensure_ascii=False,indent=2),encoding='utf8')
    return 0
if __name__=='__main__':
    raise SystemExit(main())
