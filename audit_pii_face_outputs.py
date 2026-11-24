"""Compare the 7 synthetic input cards with masked outputs.

Ground truth is synthetic; this tool validates TEXT masks and extra blackout in
synthetic panel only. It does NOT certify whether faces were all detected.
"""
from __future__ import annotations

import argparse, json
from pathlib import Path

import numpy as np
from PIL import Image


def audit(input_dir: Path, output_dir: Path):
    fixture = json.loads((input_dir / 'expected_text_and_faces.json').read_text(encoding='utf-8'))
    results = []
    for sample in fixture['samples']:
        name = sample['filename']
        inp = input_dir/name
        out = output_dir/name
        if not out.exists():
            out=output_dir/(Path(name).stem+'_masked'+Path(name).suffix)
        if not out.exists():
            print('[MISSING]',name)
            results.append({'file':name,'error':'missing output'})
            continue
        with Image.open(inp) as img, Image.open(out) as masked:
            base=img.convert('RGB')
            final=masked.convert('RGB')
            sx=final.width/base.width
            sy=final.height/base.height
            if abs(sx-sy)>0.001:
                print('[ERROR] aspect ratio changed',name)
                results.append({'file':name,'error':'aspect ratio changed'})
                continue
            base=np.asarray(base.resize(final.size,Image.Resampling.LANCZOS))
            final=np.asarray(final)
        height,width=final.shape[:2]
        # The synthetic card is 990 px wide in original input; ignore portrait.
        photo_width=round(width-990*sx)
        safe=np.zeros((height,width-photo_width),dtype=bool)
        coverages=[]
        for entity in sample['expected_text']:
            x0,y0,x1,y1=entity['bbox_px']
            x0=round(x0*sx);x1=round(x1*sx)
            y0=round(y0*sy);y1=round(y1*sy)
            region=final[y0:y1,x0:x1]
            coverage=float(np.mean(np.max(region,axis=2)<=12)) if region.size else 0
            coverages.append((entity['label'],coverage))
            pad=round(18*sx)
            a=max(photo_width,x0-pad);b=max(0,y0-pad)
            c=min(width,x1+pad);d=min(height,y1+pad)
            if c>a and d>b:
                safe[b:d,a-photo_width:c-photo_width]=True
        before=base[:,photo_width:]
        after=final[:,photo_width:]
        new_black=(np.max(after,axis=2)<=12)&(np.max(before,axis=2)>20)
        extras=int(np.count_nonzero(new_black&~safe))
        gt_good=sum(c>=0.98 for _,c in coverages)
        print(f'{name}: PII >=98% {gt_good}/{len(coverages)}; additional black pixels outside GT {extras:,}')
        results.append({'file':name,'total_text':len(coverages),'text_coverage':coverages,'non_ground_truth_black_pixels':extras})
    return results


if __name__ == '__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--input-dir',default='samples/pii_faces')
    ap.add_argument('--output-dir',default='samples/pii_faces/masked')
    ap.add_argument('--json-out',default='')
    args=ap.parse_args()
    results=audit(Path(args.input_dir),Path(args.output_dir))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results,indent=2,ensure_ascii=False),encoding='utf-8')
    print('\nNote: if non-ground-truth black pixels remain high, review output manually.')
