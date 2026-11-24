# -*- coding: utf-8 -*-
"""Create a reproducible 500-image research evaluation subset from WIDER FACE val.

Only downloads from the official CUHK-CSE HF mirror. The images stay on the
user's own machine, not redistributed in this toolkit.
"""
from __future__ import annotations
import argparse, csv, hashlib, io, json, os, random, shutil, sys, time, urllib.request, zipfile
from pathlib import Path, PurePosixPath

BASE = "https://huggingface.co/datasets/CUHK-CSE/wider_face/resolve/main/data/"
SOURCES = {
    "WIDER_val.zip": ("f9efbd09f28c5d2d884be8c0eaef3967158c866a593fc36ab0413e4b2a58a17a", 362752168),
    "wider_face_split.zip": ("c7561e4f5e7a118c249e0a5c5c902b0de90bbf120d7da9fa28d99041f68a8a5c", 3591642),
}

def file_sha(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for part in iter(lambda:f.read(1024*1024),b""): h.update(part)
    return h.hexdigest()

def verify(path, expected_hash, expected_size):
    if not path.is_file(): return False
    if path.stat().st_size != expected_size: return False
    return file_sha(path).lower()==expected_hash

def download(name, dest):
    expected_hash, expected_size = SOURCES[name]
    if verify(dest,expected_hash,expected_size):
        print(f"[OK] Existing archive verified: {dest}");return
    if dest.exists():
        raise RuntimeError(f"Existing archive is incomplete or different: {dest}. Manually move/delete it before retrying.")
    print(f"[DOWNLOAD] {name} ({expected_size / 1048576:.1f} MiB)",flush=True)
    temp=dest.with_name(dest.name+'.part')
    if temp.exists():
        raise RuntimeError(f"Partial download present: {temp}. Remove it before retrying.")
    request=urllib.request.Request(BASE+name,headers={'User-Agent':'Academic-WIDERFace-Benchmark/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=75) as response, temp.open('wb') as out:
            n=0;last=0
            while True:
                b=response.read(1024*1024)
                if not b:break
                out.write(b);n+=len(b)
                if n-last>=25*1024*1024:
                    print(f"  {name}: {n/1048576:.0f} / {expected_size/1048576:.0f} MiB",flush=True);last=n
        if not verify(temp,expected_hash,expected_size):
            raise RuntimeError(f"SHA-256/size verification failed: {name}. Do not use the file.")
        temp.replace(dest)
        print(f"[OK] Downloaded and SHA-256 verified: {name}")
    except Exception:
        if temp.exists(): temp.unlink()
        raise

def read_ground_truth(zip_file):
    with zipfile.ZipFile(zip_file) as z:
        candidates=[m for m in z.namelist() if m.endswith('wider_face_val_bbx_gt.txt')]
        if len(candidates)!=1: raise RuntimeError('Cannot locate a unique wider_face_val_bbx_gt.txt in annotation ZIP')
        raw=z.read(candidates[0]).decode('utf-8-sig')
    lines=iter(raw.splitlines())
    records=[]
    while True:
        try: filename=next(lines).strip().replace('\\','/')
        except StopIteration:break
        if not filename:continue
        p=PurePosixPath(filename)
        if p.is_absolute() or '..' in p.parts or p.suffix.lower() not in ('.jpg','.jpeg','.png'):
            raise RuntimeError(f'Unsafe or invalid image path in annotations: {filename}')
        try:count=int(next(lines).strip())
        except (StopIteration,ValueError) as e:raise RuntimeError(f'Invalid GT count for {filename}') from e
        if count<0:raise RuntimeError('Negative GT count')
        boxes=[]
        for _ in range(count):
            try:row=[int(float(x)) for x in next(lines).split()]
            except (StopIteration,ValueError) as e:raise RuntimeError('Invalid GT annotation') from e
            if len(row)<10:raise RuntimeError(f'Expected 10 WIDER GT fields for {filename}; got {len(row)}')
            x,y,w,h,blur,expression,illumination,invalid,occlusion,pose=row[:10]
            if w<=0 or h<=0:continue
            boxes.append({'bbox':[x,y,x+w,y+h], 'invalid':bool(invalid), 'blur':blur,
                          'expression':expression,'illumination':illumination,'occlusion':occlusion,'pose':pose})
        records.append({'original_relpath':filename,'faces':boxes})
    if len(records)<500: raise RuntimeError(f'Expected WIDER val images; only {len(records)} annotation records')
    unique={r['original_relpath'] for r in records}
    if len(unique)!=len(records):raise RuntimeError('Duplicate GT image paths')
    return records

def assemble(args):
    output=Path(args.output).expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f'Output exists and is not empty: {output}; use a fresh folder')
    output.mkdir(parents=True,exist_ok=True)
    downloads=Path(args.cache).expanduser().resolve()
    downloads.mkdir(parents=True,exist_ok=True)
    for name,(h,s) in SOURCES.items():
        target=downloads/name
        if args.download:download(name,target)
        elif not verify(target,h,s):
            raise RuntimeError(f'Missing/invalid {target}; run with --download, or manually download and place it here')
    records=read_ground_truth(downloads/'wider_face_split.zip')
    ordered=sorted(records,key=lambda r:r['original_relpath'])
    chosen=sorted(random.Random(args.seed).sample(ordered,args.count),key=lambda r:r['original_relpath'])
    with zipfile.ZipFile(downloads/'WIDER_val.zip') as iz:
        namelist=[n for n in iz.namelist() if n.lower().endswith(('.jpg','.jpeg','.png'))]
        indices={}
        for n in namelist:
            norm=n.replace('\\','/')
            tag='/images/'
            if tag in norm:indices[norm.split(tag,1)[1]]=n
            elif norm.startswith('images/'):indices[norm[len('images/'):]]=n
        if not indices:
            raise RuntimeError('Cannot locate WIDER_val/images/... within official image ZIP')
        img_root=output/'images';img_root.mkdir()
        new=[]
        for i,record in enumerate(chosen,1):
            rel=record['original_relpath']
            if rel not in indices:raise RuntimeError(f'Missing ZIP image: {rel}')
            local=img_root/f'face_{i:04d}{Path(rel).suffix.lower()}'
            with iz.open(indices[rel]) as source,local.open('wb') as dest:shutil.copyfileobj(source,dest)
            record=dict(record);record['file']='images/'+local.name
            new.append(record)
            if i%100==0:print(f'[EXTRACT] {i}/{args.count}',flush=True)
    gt={'schema':'widerface_val_subset_v1','source':'WIDER FACE validation images and official bounding-box ground truth',
        'subset_count':len(new),'seed':args.seed,'sampling':'Python random.Random(seed).sample of all validation images sorted by official relative path',
        'format':'bbox is [x0,y0,x1,y1] in original image pixel coordinates',
        'invalid_policy':'GT faces flagged invalid excluded from primary recall calculation; reported separately',
        'images':new}
    (output/'ground_truth.json').write_text(json.dumps(gt,ensure_ascii=False,indent=2),encoding='utf-8')
    with (output/'manifest.csv').open('w',encoding='utf-8-sig',newline='') as f:
        wr=csv.writer(f);wr.writerow(['file','original_relpath','valid_faces','invalid_faces','blurred_faces','occluded_faces','atypical_pose_faces'])
        for r in new:
            faces=r['faces']
            wr.writerow([r['file'],r['original_relpath'],sum(not x['invalid'] for x in faces),
                         sum(x['invalid'] for x in faces),sum(x['blur']>0 for x in faces),
                         sum(x['occlusion']>0 for x in faces),sum(x['pose']>0 for x in faces)])
    print('[PASS] Extracted',len(new),'original WIDER FACE val images to',output)
    print('       Valid GT faces =',sum(not x['invalid'] for r in new for x in r['faces']))
    print('       Invalid/ignored GT faces =',sum(x['invalid'] for r in new for x in r['faces']))
    print('       See',output/'ground_truth.json')

def main():
    p=argparse.ArgumentParser(description='Download official WIDER FACE val and select exactly 500 labeled images')
    p.add_argument('--output',default='WIDER_FACE_500',help='New output folder')
    p.add_argument('--cache',default='WIDER_FACE_source_zips',help='Where the official source ZIPs are stored')
    p.add_argument('--count',type=int,default=500)
    p.add_argument('--seed',type=int,default=42)
    p.add_argument('--download',action='store_true',help='Download missing official ZIP archives, with SHA256 verification')
    a=p.parse_args()
    if a.count<1 or a.count>3226:p.error('--count must be 1..3226')
    try:assemble(a)
    except Exception as e:print('[ERROR]',e,file=sys.stderr);sys.exit(1)
if __name__=='__main__':main()
