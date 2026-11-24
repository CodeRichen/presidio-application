"""Safely set RapidOCR's default ONNX Runtime CPU intra-op thread count.

Patches only `_RAPID = RapidOCR()` in `ocr_backend.py`; backup is automatic.
Use after the separate speed/PII benchmark verifies the selected value.
"""
from __future__ import annotations
import argparse
import ast
import codecs
import datetime
import pathlib
import re

START = "# RAPID_SPEED_PROFILE_BEGIN"
END = "# RAPID_SPEED_PROFILE_END"


def patch_content(text, threads):
    if threads not in (0, 2, 4, 6, 8):
        raise ValueError("CPU 執行緒只支援 0(預設)、2、4、6、8")
    tree=ast.parse(text)
    funcs=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=="_rapid_engine"]
    if len(funcs)!=1:
        raise ValueError("找不到唯一 _rapid_engine()；為避免誤改請保留原版並手動檢查")
    func=funcs[0]
    lines=text.splitlines(keepends=True)
    body="".join(lines[func.lineno-1:func.end_lineno]);newline="\r\n" if "\r\n" in text else "\n"
    if START in body:
        pat=r"(\bDEFAULT_RAPID_CPU_THREADS\s*=\s*)\d+"
        updated,n=re.subn(pat,lambda m:m.group(1)+str(threads),body)
        if n!=1:raise ValueError("現有速度優化區塊格式不同，已停止以避免誤改")
        return text.replace(body,updated,1),n>0 and updated!=body
    matches=list(re.finditer(r"(?m)^([ \t]*)_RAPID\s*=\s*RapidOCR\(\s*\)\s*$",body))
    if len(matches)!=1:
        raise ValueError("_rapid_engine() 中找不到唯一的 `_RAPID = RapidOCR()`；檔案可能已修改")
    indent=matches[0].group(1)
    block=(f"{indent}{START}{newline}"
           f"{indent}DEFAULT_RAPID_CPU_THREADS = {threads}{newline}"
           f"{indent}_threads = int(os.environ.get('PII_RAPID_CPU_THREADS', str(DEFAULT_RAPID_CPU_THREADS))){newline}"
           f"{indent}if _threads not in (0, 2, 4, 6, 8):{newline}"
           f"{indent}    raise ValueError('PII_RAPID_CPU_THREADS 只支援 0、2、4、6、8'){newline}"
           f"{indent}_params = ({{'EngineConfig.onnxruntime.intra_op_num_threads': _threads}}{newline}"
           f"{indent}           if _threads else None){newline}"
           f"{indent}_RAPID = RapidOCR(params=_params){newline}"
           f"{indent}{END}{newline}")
    changed_body=body[:matches[0].start()]+block+body[matches[0].end():]
    return text.replace(body,changed_body,1),True


def main():
    ap=argparse.ArgumentParser(description="Only apply AFTER OCR coverage/speed validation")
    ap.add_argument("--project",type=pathlib.Path,default=pathlib.Path("."))
    ap.add_argument("--threads",type=int,required=True,choices=[0,2,4,6,8])
    ap.add_argument("--apply",action="store_true")
    args=ap.parse_args()
    path=args.project.resolve()/"ocr_backend.py"
    if not path.is_file():ap.error(f"找不到：{path}")
    raw=path.read_bytes();bom=raw.startswith(codecs.BOM_UTF8)
    txt=raw.decode("utf-8-sig");patched,changed=patch_content(txt,args.threads)
    ast.parse(patched)
    print("目標：",path)
    print("CPU 執行緒：",args.threads,"（0 代表 RapidOCR 預設值）")
    print("需要修改：",changed)
    if not changed:return
    if not args.apply:
        print("目前只預覽，確認 Benchmark 沒有漏遮蔽後，增加 --apply 再執行。")
        return
    backup_dir=path.parent/".ocr_speed_backups";backup_dir.mkdir(exist_ok=True)
    stamp=datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup=backup_dir/f"ocr_backend_{stamp}.py"
    backup.write_bytes(raw)
    path.write_bytes((codecs.BOM_UTF8 if bom else b"")+patched.encode("utf-8"))
    print("已套用，備份：",backup)
    print("重新啟動 UI 後生效；不會自動修改 NER 或 PDF 模組。")


if __name__=="__main__":main()
