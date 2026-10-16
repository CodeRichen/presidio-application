import tkinter as tk
from tkinter import ttk
import main


class PrivacyUI:

    def __init__(self, root):

        self.root = root
        self.root.title("Presidio 個資去識別化工具")
        self.root.geometry("850x750")

        self.listener = None

        # =========================
        # 標題
        # =========================

        ttk.Label(
            root,
            text="Presidio 個資去識別化工具",
            font=("Arial", 20)
        ).pack(pady=15)

        # =========================
        # 狀態
        # =========================

        self.status = ttk.Label(
            root,
            text="● 剪貼簿保護：未啟動",
            font=("Arial", 13)
        )

        self.status.pack(pady=5)

        # =========================
        # 控制按鈕
        # =========================

        frame = ttk.Frame(root)
        frame.pack(pady=10)

        self.start_button = ttk.Button(
            frame,
            text="開始保護",
            command=self.start_protection
        )

        self.start_button.pack(
            side="left",
            padx=10
        )

        self.stop_button = ttk.Button(
            frame,
            text="停止保護",
            command=self.stop_protection,
            state="disabled"
        )

        self.stop_button.pack(
            side="left",
            padx=10
        )

        # =========================
        # 快速鍵資訊
        # =========================

        hotkey_frame = ttk.LabelFrame(
            root,
            text="快速鍵"
        )

        hotkey_frame.pack(
            fill="x",
            padx=30,
            pady=10
        )

        ttk.Label(
            hotkey_frame,
            text=f"複製 / 偵測：{main.HOTKEY_COPY}"
        ).pack(
            anchor="w",
            padx=15,
            pady=3
        )

        if main.DIRECT_MODE:
            paste_text = "原生 Ctrl+V"
        else:
            paste_text = main.HOTKEY_PASTE

        ttk.Label(
            hotkey_frame,
            text=f"貼上：{paste_text}"
        ).pack(
            anchor="w",
            padx=15,
            pady=3
        )

        # =========================
        # Debug Terminal
        # =========================

        debug_frame = ttk.LabelFrame(
            root,
            text="Debug / Terminal"
        )

        debug_frame.pack(
            fill="both",
            expand=True,
            padx=30,
            pady=10
        )

        self.debug_text = tk.Text(
            debug_frame,
            height=12,
            wrap="word"
        )

        debug_scroll = ttk.Scrollbar(
            debug_frame,
            orient="vertical",
            command=self.debug_text.yview
        )

        self.debug_text.configure(
            yscrollcommand=debug_scroll.set
        )

        self.debug_text.pack(
            side="left",
            fill="both",
            expand=True
        )

        debug_scroll.pack(
            side="right",
            fill="y"
        )

        # =========================
        # 遮蔽結果
        # =========================

        result_frame = ttk.LabelFrame(
            root,
            text="遮蔽後剪貼簿文字"
        )

        result_frame.pack(
            fill="both",
            expand=True,
            padx=30,
            pady=10
        )

        self.result_text = tk.Text(
            result_frame,
            height=8,
            wrap="word"
        )

        result_scroll = ttk.Scrollbar(
            result_frame,
            orient="vertical",
            command=self.result_text.yview
        )

        self.result_text.configure(
            yscrollcommand=result_scroll.set
        )

        self.result_text.pack(
            side="left",
            fill="both",
            expand=True
        )

        result_scroll.pack(
            side="right",
            fill="y"
        )

        # =========================
        # 註冊 main.py callback
        # =========================

        main.set_log_callback(
            self.receive_log
        )

        main.set_result_callback(
            self.receive_result
        )

        # =========================
        # 關閉視窗
        # =========================

        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.close
        )

    # =========================
    # 接收 Debug
    # =========================

    def receive_log(self, message):

        # pynput listener 可能不是 Tkinter 主執行緒
        # 所以使用 after() 安全更新 UI

        self.root.after(
            0,
            self._append_log,
            message
        )

    def _append_log(self, message):

        self.debug_text.insert(
            "end",
            message + "\n"
        )

        self.debug_text.see("end")

    # =========================
    # 接收遮蔽結果
    # =========================

    def receive_result(self, text):

        self.root.after(
            0,
            self._update_result,
            text
        )

    def _update_result(self, text):

        self.result_text.delete(
            "1.0",
            "end"
        )

        self.result_text.insert(
            "1.0",
            text
        )

    # =========================
    # 開始
    # =========================

    def start_protection(self):

        if self.listener is not None:
            return

        self.listener = main.create_listener()

        self.status.config(
            text="● 剪貼簿保護：已啟動"
        )

        self.start_button.config(
            state="disabled"
        )

        self.stop_button.config(
            state="normal"
        )

        self.receive_log(
            "[UI] 剪貼簿保護已啟動"
        )

    # =========================
    # 停止
    # =========================

    def stop_protection(self):

        if self.listener is None:
            return

        self.listener.stop()
        self.listener = None

        self.status.config(
            text="● 剪貼簿保護：未啟動"
        )

        self.start_button.config(
            state="normal"
        )

        self.stop_button.config(
            state="disabled"
        )

        self.receive_log(
            "[UI] 剪貼簿保護已停止"
        )

    # =========================
    # 關閉
    # =========================

    def close(self):

        if self.listener is not None:
            self.listener.stop()

        main.cleanup_map_files()

        self.root.destroy()


if __name__ == "__main__":

    root = tk.Tk()

    app = PrivacyUI(root)

    root.mainloop()