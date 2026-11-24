"""Internal shadow-only model runner. Parent never captures stdout/stderr."""
from __future__ import annotations
import argparse,hashlib,json,os,sys,time
from pathlib import Path
from PIL import ImageStat,ImageFilter
from v62_core import Features, decide, audit_gt, metrics_from_words


def quality(img):
    # Do not serialize original image/pixels. Approximate whole-page quality indicators.
    tiny=img.copy();tiny.thumbnail((500,500))
    grey=tiny.convert('L')
    std=float(ImageStat.Stat(grey).stddev[0]); edge=float(ImageStat.Stat(grey.filter(ImageFilter.FIND_EDGES)).mean[0])
    return round(std,3),round(edge,3)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--root',required=True)
    ap.add_argument('--fixture',required=True)
    ap.add_argument('--gt',required=True)
    ap.add_argument('--sha256',required=True)
    ap.add_argument('--report',required=True)
    ap.add_argument('--profile',choices=('none','legacy_synthetic'),default='none')
    args=ap.parse_args()
    root=Path(args.root).resolve();fixture=Path(args.fixture).resolve()
    if hashlib.sha256(fixture.read_bytes()).hexdigest()!=args.sha256:
        raise RuntimeError('fixture changed')
    os.environ.update({
        'PII_OCR_BACKEND':'rapid','PII_NER_BACKEND':'ac',
        'PII_MEDIA_FP_REPORT':'0','PII_MEDIA_CACHE':'1','PII_ZH_SINGLE_PASS':'1',
        'PII_FACE_ENABLED':'0', 'PII_MEDIA_FP_PROFILE':('synthetic_face_test' if args.profile=='legacy_synthetic' else ''),
    })
    sys.path.insert(0,str(root))
    import handler_media as hm
    try: import pymupdf as fitz
    except ImportError: import fitz
    rows=json.loads(Path(args.gt).read_text(encoding='utf-8'))
    if not isinstance(rows,list): raise RuntimeError('invalid ground truth')
    results=[]
    with fitz.open(str(fixture)) as doc:
        if len(doc)!=len(rows) or any(p.rotation for p in doc): raise RuntimeError('unexpected PDF')
        for i,page in enumerate(doc):
            # Compare both zoom levels, but candidate selection uses ONLY 2x features.
            obs={'zoom':None,'words':0,'min_pt':0.0}
            old_ocr=hm.ocr_words
            def spy(img):
                found=old_ocr(img)
                if obs['zoom']==2.0:
                    obs['words'],obs['min_pt']=metrics_from_words(found,2.0)
                return found
            hm.ocr_words=spy
            try:
                @hm._pii_media_cache_scope
                def run_ocr():
                    obs['zoom']=2.0
                    t=time.perf_counter()
                    primary,im=hm._collect_pdf_ocr_boxes(page,2.0)
                    elapsed2=time.perf_counter()-t
                    contrast,edge=quality(im)
                    feat=Features(obs['words'],obs['min_pt'],len(primary),contrast,edge)
                    would_skip,reasons=decide(feat)
                    del im
                    obs['zoom']=3.0
                    t=time.perf_counter()
                    fallback,im3=hm._collect_pdf_ocr_boxes(page,3.0)
                    elapsed3=time.perf_counter()-t
                    del im3
                    merged=hm._merge_pdf_multizoom_boxes(primary,fallback)
                    chosen=primary if would_skip else merged
                    return feat,would_skip,reasons,primary,merged,chosen,elapsed2,elapsed3
                feat,skipped,reasons,primary,merged,chosen,t2,t3=run_ocr()
            finally:
                hm.ocr_words=old_ocr
            gt=rows[i]['expected_text']
            a=audit_gt(gt,primary)
            b=audit_gt(gt,merged)
            c=audit_gt(gt,chosen)
            all_added=max(0,len(merged)-len(primary))
            row={
              'page':i+1,'case':rows[i]['case'],
              'ground_truth_count':len(gt),'zoom2':a,'dual':b,'selective_shadow':c,
              '2x_words':feat.words,'min_word_height_pt':feat.min_word_height_pt,
              '2x_pii_boxes':len(primary),'dual_pii_boxes':len(merged),
              '3x_extra_boxes':all_added,
              'estimated_contrast_std':feat.contrast_std,'estimated_edge_mean':feat.edge_mean,
              'would_skip_3x':skipped,'reasons':reasons,
              'zoom2_s':round(t2,3),'zoom3_s':round(t3,3),
              'omitted_3x_s_estimate':round(t3 if skipped else 0.0,3),
              'new_miss_against_dual':max(0,b['pass_count']-c['pass_count']),
            }
            results.append(row)
    Path(args.report).write_text(json.dumps({'cases':results},ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':
    main()
