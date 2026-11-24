"""Safely append a V5 switch to an existing pdf_secure_output.py, preserving V3.
Usage: python PII_PDF_V5_隔離輸出工具包/install_v5.py --check|--apply|--remove
"""
import argparse
import ast
from datetime import datetime
import os
from pathlib import Path
import shutil

BEGIN = '# BEGIN PII_PDF_V5_PROCESS_ISOLATION'
END = '# END PII_PDF_V5_PROCESS_ISOLATION'
PATCH = '''\n\n# BEGIN PII_PDF_V5_PROCESS_ISOLATION
_PII_V5_ORIGINAL_SAVE = save_secure_raster_pdf

def save_secure_raster_pdf(masked_doc, output_path: str, *, dpi: int = 200):
    import os as _pii_v5_os
    if _pii_v5_os.environ.get("PII_PDF_EXPORT_V5", "0") == "1":
        from pdf_secure_v5 import save_secure_raster_pdf_v5
        return save_secure_raster_pdf_v5(
            masked_doc, output_path, dpi=dpi,
            verify_fn=_verify_secure_raster_pdf,
        )
    return _PII_V5_ORIGINAL_SAVE(masked_doc, output_path, dpi=dpi)
# END PII_PDF_V5_PROCESS_ISOLATION
'''
HELPERS = ['pdf_secure_v5.py','pdf_secure_v5_worker.py']


def locate_root(arg):
    return Path(arg).resolve()


def inspect(path):
    content = path.read_text(encoding='utf-8')
    if content.count(BEGIN) != content.count(END):
        raise ValueError('V5 修改標記不完整，拒絕修改')
    if BEGIN in content:
        if content.count(BEGIN) != 1:
            raise ValueError('重複的 V5 修改標記，拒絕修改')
        return content, True
    tree = ast.parse(content)
    func_names = {node.name for node in tree.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))}
    required = {'save_secure_raster_pdf','_verify_secure_raster_pdf'}
    if not required.issubset(func_names):
        raise ValueError('現有 PDF 程式缺少必要入口，拒絕修改')
    compile(content + PATCH, str(path), 'exec')
    return content, False


def backup(path):
    label=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    target=path.with_name(path.name+f'.bak_v5_{label}')
    shutil.copy2(path,target)
    return target


def install(root:Path, mode:str):
    path = root / 'pdf_secure_output.py'
    if not path.is_file():
        raise FileNotFoundError(f'找不到 {path}')
    content, applied = inspect(path)
    src = Path(__file__).resolve().parent
    for name in HELPERS:
        if not (src/name).is_file():
            raise FileNotFoundError(f'工具包缺少 {name}')
    if mode == 'check':
        print('[PASS] 現有 PDF 入口相容。'+ ('V5 已安裝。' if applied else '可套用 V5，尚未修改檔案。'))
        return
    if mode == 'remove':
        if not applied:
            print('[INFO] V5 尚未安裝，沒有修改。')
            return
        b=content.index(BEGIN); e=content.index(END)+len(END)
        new=content[:b].rstrip()+content[e:]
        ast.parse(new)
        bk=backup(path)
        path.write_text(new.rstrip()+'\n',encoding='utf-8')
        print('[PASS] 已移除 V5 入口，其他程式保持不變；備份：'+str(bk))
        return
    if applied:
        print('[INFO] V5 已安裝，未覆蓋或重複套用。')
        return
    for name in HELPERS:
        dest=root/name
        if dest.exists() and dest.read_bytes() != (src/name).read_bytes():
            raise RuntimeError(f'{name} 已存在不同版本，拒絕覆蓋，請先備份並檢查')
    b=backup(path)
    for name in HELPERS:
        dest=root/name
        if not dest.exists():
            shutil.copy2(src/name,dest)
    path.write_text(content.rstrip()+PATCH,encoding='utf-8')
    ast.parse(path.read_text(encoding='utf-8'))
    print('[PASS] 已安裝 V5；原檔備份：'+str(b))
    print('[INFO] 預設關閉，只有設定 PII_PDF_EXPORT_V5=1 才啟用。')


def main():
    ap=argparse.ArgumentParser()
    grp=ap.add_mutually_exclusive_group(required=True)
    grp.add_argument('--check',action='store_true')
    grp.add_argument('--apply',action='store_true')
    grp.add_argument('--remove',action='store_true')
    ap.add_argument('--root',default='.',help='presidio_app_allen 專案根目錄')
    args=ap.parse_args()
    install(locate_root(args.root),'check' if args.check else 'apply' if args.apply else 'remove')

if __name__=='__main__':
    main()
