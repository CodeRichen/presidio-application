"""啟動展示版 GUI；從專案根目錄匯入原本的模組，完全不覆蓋 ui.py。"""
from pathlib import Path
import os
import runpy
import sys


def main():
    here = Path(__file__).resolve().parent
    project = here.parent
    required = ("ui.py", "core.py", "app_env.py")
    absent = [name for name in required if not (project / name).is_file()]
    if absent:
        raise SystemExit("請將整個 PII_UI_Demo_Refresh 資料夾放在含 ui.py/core.py 的專案根目錄。缺少：" + ", ".join(absent))
    sys.path.insert(0, str(project))
    os.chdir(project)
    runpy.run_path(str(here / "ui_demo.py"), run_name="__main__")


if __name__ == "__main__":
    main()
