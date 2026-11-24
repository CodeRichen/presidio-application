#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Launch actual ui.App with timing instrumentation; do not edit ui.py.

Click '檔案' -> '開始處理'. Records click -> fully updated file-output list.
No PII, input filenames, OCR text, maps or original crops stored in timing JSONL.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import functools
import json
import os
from pathlib import Path
import sys
import threading
import time


def main(argv=None):
    parser=argparse.ArgumentParser(description='實際 UI 按鈕到輸出清單完成的端到端測速')
    parser.add_argument('--project',type=Path,default=Path.cwd(),help='presidio_app_allen 專案根目錄')
    parser.add_argument('--report',type=Path,default=None,help='JSONL 統計檔；不儲存個資')
    args=parser.parse_args(argv)
    root=args.project.resolve()
    if not (root/'ui.py').is_file() or not (root/'core.py').is_file():
        parser.error('專案根目錄內找不到 ui.py 或 core.py')
    report=(args.report.resolve() if args.report else Path(__file__).resolve().parent/'results'/'ui_timing.jsonl')
    report.parent.mkdir(parents=True,exist_ok=True)
    os.chdir(root)
    sys.path.insert(0,str(root))

    import handler_media
    import ui

    local=threading.local()

    def wrap_phase(target,name,phase):
        orig=getattr(target,name,None)
        if not callable(orig):return
        @functools.wraps(orig)
        def wrapped(*a,**kw):
            started=time.perf_counter()
            try:return orig(*a,**kw)
            finally:
                cur=getattr(local,'current',None)
                if cur is not None:
                    cur['phases'][phase]['seconds'] += time.perf_counter()-started
                    cur['phases'][phase]['calls'] += 1
        setattr(target,name,wrapped)

    wrap_phase(handler_media,'ocr_words','ocr')
    wrap_phase(handler_media,'find_sensitive_boxes_in_words','pii')
    wrap_phase(handler_media,'_save_map_entry','save_map')
    wrap_phase(handler_media,'mask_image','mask_image')
    wrap_phase(handler_media,'mask_pdf','mask_pdf')

    class TimedApp(ui.App):
        def __init__(self):
            self._ui_bench_click=None
            self._ui_bench_input_ext={}
            super().__init__()

        def run_files(self):
            if not self.busy and self.files:
                self._ui_bench_click=time.perf_counter()
                self._ui_bench_input_ext=dict(Counter(Path(p).suffix.lower() for p in self.files))
            return super().run_files()

        def _start(self, func, msg, on_done):
            # Only measure the actual file button; NOT model warmup, text, or folder scans.
            if msg!='處理檔案中…' or self._ui_bench_click is None or self.busy:
                return super()._start(func,msg,on_done)

            click=self._ui_bench_click
            file_types=self._ui_bench_input_ext
            self._ui_bench_click=None
            snapshot={ 'phases': {p:{'seconds':0.0,'calls':0} for p in
                        ('ocr','pii','save_map','mask_image','mask_pdf')}}
            worker_state={}

            def timed_job():
                local.current=snapshot
                worker_state['started']=time.perf_counter()
                try:
                    return func()  # actual UI job: core.dispatch_files(paths, self.mapping)
                except Exception:
                    worker_state['error']=True
                    raise
                finally:
                    worker_state['finished']=time.perf_counter()
                    local.current=None
                    if worker_state.get('error'):
                        # Error path has no on_done; worker time is recorded, but not UI display end.
                        write_record(dict(status='WORKER_ERROR', timestamp=datetime.now().astimezone().isoformat(timespec='seconds'),
                                          file_types=file_types, worker_s=round(worker_state['finished']-worker_state['started'],4),
                                          phases=snapshot['phases']))

            def write_record(record):
                with report.open('a',encoding='utf-8') as f:
                    f.write(json.dumps(record,ensure_ascii=False,sort_keys=True)+'\n')

            def timed_done(outs):
                # on_done updates the Listbox and refreshes lists. Time AFTER it finishes.
                if on_done:
                    on_done(outs)
                finished=time.perf_counter()
                record=dict(
                    status='OK',timestamp=datetime.now().astimezone().isoformat(timespec='seconds'),
                    file_types=file_types,
                    outputs=len(outs),
                    click_to_ui_s=round(finished-click,4),
                    worker_s=round(worker_state['finished']-worker_state['started'],4),
                    wait_and_ui_s=round((worker_state['started']-click)+(finished-worker_state['finished']),4),
                    phases=snapshot['phases'],
                )
                write_record(record)
                print('[UI 端到端測速] 按下開始處理→輸出清單完成：'
                      f"{record['click_to_ui_s']:.3f} 秒；背景工作：{record['worker_s']:.3f} 秒")
                print(f'[UI 測速報告] {report}')
                # Avoid showing the filename or confidential OCR text in the report.

            return super()._start(timed_job,msg,timed_done)

    print('[準備] 啟動原有 UI；請先等待模型預熱完成後再操作')
    print('[提示] 在「檔案」分頁加入合成測試圖片／PDF，再按「開始處理」')
    print('[報告] '+str(report))
    TimedApp().mainloop()
    return 0

if __name__=='__main__':raise SystemExit(main())
