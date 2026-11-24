#!/usr/bin/env python3
# coding: utf-8
"""Privacy-safe cProfile sampling of existing OCR words call, only fixed synthetic fixture.

Never serialize callable args, OCR text, image, full filesystem paths, or raw pstats.
"""
import cProfile
from functools import wraps
import os
import pstats
import time
from collections import Counter
from v661_runtime import _active


def categorize(module, func):
    label = (module + ' ' + func).lower()
    if 'onnxruntime' in label or 'inferencesession' in label:
        return 'onnxruntime'
    if 'rapidocr' in label:
        return 'rapidocr'
    if 'cv2' in label or 'opencv' in label:
        return 'opencv'
    if 'numpy' in label or module.lower().startswith(('numeric','multiarray')):
        return 'numpy'
    if module.lower().startswith('image') or 'pillow' in label or 'pil.' in label:
        return 'pillow'
    if 'torch' in label:
        return 'torch'
    return 'other'


def safe_stats(prof, limit=20):
    """Return only module basenames, static function identifiers and numeric counters."""
    raw = pstats.Stats(prof).stats
    rows = []
    for (filename, _line, func), (pcalls, ncalls, self_s, cum_s, _callers) in raw.items():
        module = os.path.basename(filename) if filename not in ('~', '') else '<builtin>'
        if len(module) > 90: module = module[:90]
        if len(func) > 110: func = func[:110]
        rows.append({'module_file':module, 'function':func,
                     'calls':int(ncalls), 'self_s':round(float(self_s),6),
                     'cumulative_s':round(float(cum_s),6),
                     'category':categorize(module,func)})
    self_best=sorted(rows,key=lambda x:-x['self_s'])[:limit]
    cum_best=sorted(rows,key=lambda x:-x['cumulative_s'])[:limit]
    # Summing *self* times is meaningful; cumulative is nested and is never summed.
    cat=Counter()
    for row in rows: cat[row['category']] += row['self_s']
    return {'top_self_time':self_best,'top_cumulative_time':cum_best,
            'self_time_by_category_s':{k:round(v,5) for k,v in sorted(cat.items())},
            'unique_static_functions':len(rows)}


class OCRHotspots:
    def __init__(self, sample_every=2):
        if not isinstance(sample_every,int) or sample_every<1 or sample_every>7:
            raise ValueError('sample_every must be 1..7')
        self.sample_every=sample_every
        self._original=None
        self._owner=None
        self._profiles=[]
        self._counts=Counter()
        self._sampled=Counter()
        self._sample_wall=Counter()

    def install(self, hm):
        if not callable(getattr(hm,'ocr_words',None)):
            raise RuntimeError('MISSING_OCR_WORDS')
        if self._original is not None:
            raise RuntimeError('ALREADY_INSTALLED')
        original=hm.ocr_words
        self._original=original
        self._owner=hm
        @wraps(original)
        def wrapped(*args, **kwargs):
            active=_active.get()
            if active is None:
                return original(*args,**kwargs)
            zoom=float(active['zoom'])
            page=int(active['page'])
            key=str(int(zoom))+'x'
            self._counts[key]+=1
            if ((page-1)%self.sample_every)!=0:
                return original(*args,**kwargs)
            self._sampled[key]+=1
            prof=cProfile.Profile()
            start=time.perf_counter()
            try:
                prof.enable()
                return original(*args,**kwargs)
            finally:
                prof.disable()
                self._sample_wall[key]+=time.perf_counter()-start
                self._profiles.append(prof)
        hm.ocr_words=wrapped
        return self

    def restore(self):
        if self._original is not None:
            self._owner.ocr_words=self._original
            self._owner=None
            self._original=None

    def summarize(self):
        combined=cProfile.Profile()
        # pstats.add() works with in-memory Stats and does not persist raw profile.
        # Merge pstats dict entries directly via Stats.add so call counts accumulate.
        if self._profiles:
            aggregate=pstats.Stats(self._profiles[0])
            for profile in self._profiles[1:]:
                aggregate.add(pstats.Stats(profile))
            # Reuse compact sanitizer on a Stats instance (same .stats contract).
            stats=safe_stats_from_raw(aggregate.stats)
        else:
            stats={'top_self_time':[],'top_cumulative_time':[],
                   'self_time_by_category_s':{},'unique_static_functions':0}
        return {'schema':'pii_v661_ocr_hotspots_1',
                'sample_every_page':self.sample_every,
                'calls_by_zoom':dict(self._counts),
                'profiled_calls_by_zoom':dict(self._sampled),
                'sampled_ocr_wall_s_by_zoom':{k:round(v,4) for k,v in self._sample_wall.items()},
                **stats,
                'warning':'cProfile adds overhead. Cumulative times overlap. Native inference work may not split into detection/recognition. No OCR text or images saved.'}


def safe_stats_from_raw(raw,limit=20):
    rows=[]
    for (filename,_line,func),(pcalls,ncalls,self_s,cum_s,_callers) in raw.items():
        module=os.path.basename(filename) if filename not in ('~','') else '<builtin>'
        module=module[:90];func=func[:110]
        rows.append({'module_file':module,'function':func,
                     'calls':int(ncalls),'self_s':round(float(self_s),6),
                     'cumulative_s':round(float(cum_s),6),
                     'category':categorize(module,func)})
    cat=Counter()
    for row in rows:cat[row['category']]+=row['self_s']
    return {'top_self_time':sorted(rows,key=lambda x:-x['self_s'])[:limit],
            'top_cumulative_time':sorted(rows,key=lambda x:-x['cumulative_s'])[:limit],
            'self_time_by_category_s':{k:round(v,5) for k,v in sorted(cat.items())},
            'unique_static_functions':len(rows)}
