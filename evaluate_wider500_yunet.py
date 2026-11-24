# -*- coding: utf-8 -*-
"""Independent WIDER FACE subset evaluation using the PROJECT'S face_detector.py.
Do not pad predictions during detection evaluation. Results are NOT official
WIDER FACE AP (Easy/Medium/Hard): this is fixed-threshold subset P/R/F1.
"""
from __future__ import annotations
import argparse, csv, json, math, statistics, sys, time
from pathlib import Path

def iou(a,b):
    x0=max(a[0],b[0]);y0=max(a[1],b[1]);x1=min(a[2],b[2]);y1=min(a[3],b[3])
    inter=max(0,x1-x0)*max(0,y1-y0)
    area_a=max(0,a[2]-a[0])*max(0,a[3]-a[1]);area_b=max(0,b[2]-b[0])*max(0,b[3]-b[1])
    u=area_a+area_b-inter
    return inter/u if u else 0.0

def maximum_match(gt,preds,threshold):
    edges=[]
    for g in gt:
        c=[(j,iou(g,p)) for j,p in enumerate(preds) if iou(g,p)>=threshold]
        edges.append([j for j,_ in sorted(c,key=lambda x:(-x[1],x[0]))])
    matches={}
    def aug(g,seen):
        for p in edges[g]:
            if p in seen:continue
            seen.add(p)
            if p not in matches or aug(matches[p],seen):
                matches[p]=g;return True
        return False
    for g in sorted(range(len(gt)),key=lambda k:(len(edges[k]),k)):aug(g,set())
    return matches

def evaluate(args):
    from PIL import Image
    from face_detector import detect_faces,padded_face_box,face_enabled
    if not face_enabled():raise RuntimeError('PII_FACE_ENABLED is disabled; enable your actual YuNet detector')
    root=Path(args.dataset).expanduser().resolve()
    gt=json.loads((root/'ground_truth.json').read_text(encoding='utf-8-sig'))
    if gt.get('schema')!='widerface_val_subset_v1':raise ValueError('Unexpected GT schema')
    output=Path(args.output).expanduser().resolve()
    if output.exists():raise FileExistsError(f'Output already exists: {output}')
    output.parent.mkdir(parents=True,exist_ok=True)
    all_tp=all_fp=all_fn=ignored_total=ignored_pred=0
    times=[];records=[]
    items=gt['images']
    if len(items)!=gt['subset_count']:raise ValueError('GT subset count inconsistent')
    for ix,item in enumerate(items,1):
        rel=item['file'].replace('\\','/')
        p=root/rel
        if not p.is_file() or p.resolve().parent!= (root/'images').resolve():raise FileNotFoundError('Invalid image file '+rel)
        with Image.open(p) as im: img=im.convert('RGB')
        t0=time.perf_counter()
        raw=detect_faces(img)
        elapsed=time.perf_counter()-t0
        times.append(elapsed)
        preds=[]
        for face in raw:
            rect=padded_face_box(face,img.size,padding=0.0)
            if len(rect)!=4:raise ValueError('Unexpected padded_face_box shape')
            x0,y0,x1,y1=map(float,rect)
            if x1<=x0 or y1<=y0:continue
            preds.append([x0,y0,x1,y1])
        valid=[f['bbox'] for f in item['faces'] if not f['invalid']]
        ignored=[f['bbox'] for f in item['faces'] if f['invalid']]
        matched=maximum_match(valid,preds,args.iou)
        neutral=sum(any(iou(preds[j],b)>=args.iou for b in ignored)
                    for j in range(len(preds)) if j not in matched)
        tp=len(matched);fn=len(valid)-tp;fp=len(preds)-tp-neutral
        if fp<0:raise RuntimeError('Negative FP')
        all_tp+=tp;all_fp+=fp;all_fn+=fn;ignored_total+=len(ignored);ignored_pred+=neutral
        records.append({'image':rel,'gt_valid':len(valid),'gt_invalid':len(ignored),'detections':len(preds),
                        'tp':tp,'fp':fp,'fn':fn,'ignored_detections':neutral,'elapsed_s':round(elapsed,5)})
        if ix%25==0:print(f'[EVAL] {ix}/{len(items)} | TP={all_tp} FP={all_fp} FN={all_fn}',flush=True)
    precision=all_tp/(all_tp+all_fp) if all_tp+all_fp else 0
    recall=all_tp/(all_tp+all_fn) if all_tp+all_fn else 0
    f1=2*precision*recall/(precision+recall) if precision+recall else 0
    result={'description':'YuNet raw detection on 500-image WIDER FACE validation subset',
      'notice':'Not official WIDER FACE AP. Invalid GT excluded; unmatched predictions overlapping invalid GT at IoU threshold are neutral.',
      'data_source':'WIDER FACE validation subset','dataset_seed':gt['seed'],'images':len(items),'iou_threshold':args.iou,
      'totals':{'gt_valid_faces':all_tp+all_fn,'gt_invalid_faces':ignored_total,'tp':all_tp,'fp':all_fp,'fn':all_fn,
                'neutral_predictions_on_invalid_gt':ignored_pred,'precision':precision,'recall':recall,'f1':f1,
                'mean_detection_s':statistics.mean(times),'median_detection_s':statistics.median(times)},
      'per_image':records}
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    md=output.with_suffix('.md')
    md_content = f"""# WIDER FACE 500｜YuNet 人臉偵測 Benchmark

資料集：WIDER FACE validation，seed={gt['seed']}，隨機固定抽樣 {len(items)} 張圖片。

評估：原始 YuNet 偵測框，IoU ≥ {args.iou}，一對一配對；不計黑框 padding。

| 指標 | 結果 |
|---|---:|
| 有效 GT 人臉數 | {all_tp+all_fn} |
| 忽略標註人臉數 | {ignored_total} |
| TP | {all_tp} |
| FP | {all_fp} |
| FN | {all_fn} |
| Precision | {precision*100:.2f}% |
| Recall | {recall*100:.2f}% |
| F1 | {f1:.4f} |
| 平均純偵測耗時 | {statistics.mean(times):.3f} 秒／張 |
| 中位純偵測耗時 | {statistics.median(times):.3f} 秒／張 |

**提醒：** 這是 500 張子集的固定 IoU/信心門檻 PRF1，不等於 WIDER FACE 官方 Easy/Medium/Hard AP；未驗證最終遮蔽像素覆蓋。
"""
    md.write_text(md_content,encoding='utf-8')
    print('[DONE] images=',len(items),'GT=',all_tp+all_fn,'TP=',all_tp,'FP=',all_fp,'FN=',all_fn)
    print(f'       Precision={precision*100:.2f}% Recall={recall*100:.2f}% F1={f1:.4f}')
    print('[JSON]',output,'[CANVA MD]',md)

def main():
    p=argparse.ArgumentParser(description='Evaluate face_detector.py on WIDER FACE 500 subset')
    p.add_argument('--dataset',default='WIDER_FACE_500')
    p.add_argument('--output',default='wider500_yunet_results.json')
    p.add_argument('--iou',type=float,default=.5)
    a=p.parse_args()
    if not 0<a.iou<=1:p.error('--iou must be in (0,1]')
    try:evaluate(a)
    except Exception as e:print('[ERROR]',str(e),file=sys.stderr);sys.exit(1)
if __name__=='__main__':main()
