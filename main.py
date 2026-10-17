"""
剪貼簿敏感資訊防護 - 主程式 (全域快速鍵，命令列版)
====================================================
快速鍵與模式 A/B 的流程都在 hotkey_service.py，這支只負責：
載入對照表 -> 預熱模型 -> 啟動快速鍵服務 -> 等待中止 -> 清理對照表。

快速鍵設定：
    讀取同資料夾的 hotkeys.json (ui.py 的「快速鍵」分頁會寫這個檔)；
    沒有檔案時使用 hotkey_service.DEFAULT_CONFIG。
    paste 留空 (null) 即為模式 B：copy 設成 "<ctrl>+c"，複製時直接覆寫剪貼簿。

架構 (main 與 ui 互相獨立，彼此不 import)：
    core.py            <- 共用核心 (遮蔽/還原、分派到 code/text/media/field/csv、剪貼簿工具)
    hotkey_service.py  <- 共用的快速鍵服務與設定檔
    main.py            <- 這支
    ui.py              <- 圖形介面

安裝：pip install pywin32 psutil pyperclip pynput
"""
import time

import core
import hotkey_service


def main():
    mapping = core.load_map()
    core.warmup()   # 先載入模型，再開始監聽

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