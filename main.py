"""
剪貼簿敏感資訊防護 - 主程式 (全域快速鍵，命令列版)
====================================================
快速鍵與模式 A/B 的流程都在 hotkey_service.py，這支只負責：
載入對照表 -> 預熱模型 -> 啟動快速鍵服務 -> 等待中止 -> 清理對照表。

快速鍵設定：
    讀取同資料夾的 hotkeys.json (ui.py 的「快速鍵」分頁會寫這個檔)；
    沒有檔案時使用 hotkey_service.DEFAULT_CONFIG。
    paste 留空 (null) 即為模式 B：copy 設成 "<ctrl>+c"，複製時直接覆寫剪貼簿。

人臉遮蔽 (圖片 / PDF)：
    預設開啟，由 handler_media 以本機 YuNet 離線偵測；也可用環境變數 PII_FACE_ENABLED=0 關閉。
    命令列：python main.py --no-face   本次執行關閉人臉遮蔽
            python main.py --face      本次執行強制開啟 (覆蓋環境變數)
    開啟時啟動前會檢查 opencv-python 與 YuNet 模型，並預先載入模型，
    避免第一張圖片才載入而變慢。缺模型時處理圖片 / PDF 會直接報錯 (fail closed)，
    不會輸出沒遮人臉的檔案。

架構 (main 與 ui 互相獨立，彼此不 import)：
    core.py            <- 共用核心 (遮蔽/還原、分派到 code/text/media/field/csv、剪貼簿工具)
    hotkey_service.py  <- 共用的快速鍵服務與設定檔
    main.py            <- 這支
    ui.py              <- 圖形介面

安裝：pip install pywin32 psutil pyperclip pynput opencv-python
"""
import os
import sys
import time
import importlib
import importlib.util

import core
import hotkey_service

MEDIA_MODULE = "handler_media"   # 圖片/PDF 去識別化模組 (含人臉遮蔽)
_FACE_OFF = {"0", "off", "false", "no"}


def apply_face_args():
    """命令列 --no-face / --face 覆蓋環境變數 (handler_media 每次處理都會重新讀取)"""
    argv = sys.argv[1:]
    if "--no-face" in argv:
        os.environ["PII_FACE_ENABLED"] = "0"
    elif "--face" in argv:
        os.environ["PII_FACE_ENABLED"] = "1"


def face_enabled():
    return os.environ.get("PII_FACE_ENABLED", "1").strip().lower() not in _FACE_OFF


def check_face_ready():
    """回傳 (是否可用, 說明)。可用時順便預先載入 YuNet 模型並回傳載入耗時。"""
    if importlib.util.find_spec("cv2") is None:
        return False, "尚未安裝 opencv-python (pip install opencv-python)"
    try:
        hm = importlib.import_module(MEDIA_MODULE)
    except Exception as e:
        return False, f"無法載入 {MEDIA_MODULE}：{e}"
    try:
        t = time.perf_counter()
        hm.detect_faces(hm.Image.new("RGB", (64, 64)))   # 空白圖：只為了載入模型
        return True, f"YuNet 模型已預先載入 ({(time.perf_counter() - t) * 1000:.0f} ms)"
    except Exception as e:   # 找不到模型檔等
        return False, str(e)


def main():
    apply_face_args()
    mapping = core.load_map()
    core.warmup()   # 先載入模型，再開始監聽

    if face_enabled():
        ok, msg = check_face_ready()
        if ok:
            print(f"[人臉遮蔽] 已開啟：圖片 / PDF 會遮蔽人臉。{msg}")
        else:
            print(f"[警告] 人臉遮蔽已開啟但環境不完整：{msg}")
            print("       處理含圖片 / PDF 的檔案時會直接失敗 (為了安全不會略過人臉)；"
                  "請補齊後重新啟動，或加上 --no-face 關閉。")
    else:
        print("[人臉遮蔽] 已關閉：圖片 / PDF 只遮蔽文字類敏感資訊")

    service = hotkey_service.HotkeyService(mapping)
    try:
        service.start()
    except ValueError as e:
        print(f"[錯誤] 快速鍵設定不合法：{e}")
        print(f"請修改 {hotkey_service.CONFIG_FILE} 或用 ui.py 重新設定")
        return

    if service.direct_mode:
        print("提醒：終端機按 Ctrl+C 若無法中止腳本，直接關閉終端機視窗即可\n")
    else:
        print("在終端機視窗按 Ctrl+C 可停止腳本\n")

    try:
        while service.running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n[結束] 收到中斷信號，正在關閉監聽...")
    finally:
        service.stop()
        core.cleanup_map_files()


if __name__ == "__main__":
    main()