# -*- coding: utf-8 -*-
"""Apply a small, guarded optimization to the EXISTING ui.py.

Only patches QueueWriter and App._poll; does not replace the user's whole UI.
Run from project root using your venv Python: python apply_ui_optimization.py
"""
import argparse
import ast
import datetime
import os
from pathlib import Path
import tempfile

MARKER = "# BEGIN GUI_LOG_QUEUE_OPTIMIZATION_V1"

CONFIG_BLOCK = '''# BEGIN GUI_LOG_QUEUE_OPTIMIZATION_V1
# GUI updates are time-bounded and batched; media processing remains in its worker.
GUI_LOG_POLL_MAX_ITEMS = 250
GUI_LOG_POLL_BUDGET_MS = 12.0
GUI_LOG_QUEUE_LIMIT = 12000       # Backpressure only for display logs
GUI_LOG_MAX_RECORD_CHARS = 16000  # Do not put arbitrarily large writes in Tk
GUI_LOG_MAX_LINES = 3000          # Bounded on-screen history
# END GUI_LOG_QUEUE_OPTIMIZATION_V1
'''

QUEUE_WRITER_BLOCK = '''class QueueWriter:
    """Route stdout to UI, without blocking a busy worker on an overloaded log queue.

    Only the GUI history is limited. PII detection and saved outputs are unchanged.
    """
    def __init__(self, q):
        self.q = q
        self._drop_lock = threading.Lock()
        self._dropped = 0

    def write(self, s):
        if not s:
            return
        if len(s) > GUI_LOG_MAX_RECORD_CHARS:
            s = s[:GUI_LOG_MAX_RECORD_CHARS] + "\\n[GUI] 單筆日誌過長，顯示部分已省略。\\n"
        with self._drop_lock:
            # Keep important errors visible under extreme logging load.
            important = s.lstrip().startswith(("[錯誤]", "[PDF安全檢查]", "[PDF安全輸出]"))
            if not important and self.q.qsize() >= GUI_LOG_QUEUE_LIMIT:
                self._dropped += 1
                if self._dropped == 1:
                    self.q.put(("log", "\\n[GUI] 畫面日誌過量，部分內容已略過；不影響實際遮蔽結果。\\n"))
                return
            if self._dropped:
                self.q.put(("log", f"\\n[GUI] 佇列過載，省略 {self._dropped} 段畫面日誌（不影響實際處理）。\\n"))
                self._dropped = 0
            self.q.put(("log", s))

    def flush(self):
        pass
'''

POLL_BLOCK = '''    def _append_log_batch(self, chunks):
        """One Tk insert per batch; keep UI history bounded to avoid growing costs."""
        if not chunks:
            return
        self.log.configure(state="normal")
        try:
            self.log.insert("end", "".join(chunks))
            try:
                line_count = int(self.log.index("end-1c").split(".")[0])
                if line_count > GUI_LOG_MAX_LINES:
                    self.log.delete("1.0", f"{line_count - GUI_LOG_MAX_LINES + 1}.0")
            except (ValueError, IndexError):
                pass
            self.log.see("end")
        finally:
            self.log.configure(state="disabled")

    def _poll(self):
        """Drain only a bounded amount per tick. UI never loops forever on logs."""
        started = time.perf_counter()
        chunks = []
        handled = 0
        while handled < GUI_LOG_POLL_MAX_ITEMS:
            if handled and (time.perf_counter() - started) * 1000 >= GUI_LOG_POLL_BUDGET_MS:
                break
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            handled += 1
            if item[0] == "log":
                chunks.append(item[1])
            elif item[0] == "status":
                if self.busy:
                    self.status.set(item[1])
            elif item[0] == "done":
                # Show preceding worker output before completion/errors.
                self._append_log_batch(chunks)
                chunks.clear()
                _, result, err = item
                self._set_busy(False)
                cb, self._on_done = self._on_done, None
                if err:
                    print(f"[錯誤] {err}")
                    messagebox.showerror("處理失敗", str(err))
                elif cb:
                    cb(result)
                self.refresh_map_view()
        self._append_log_batch(chunks)

        if len(self.mapping) != self._map_n:
            self.refresh_map_view()
        self._tick += 1
        if self._tick % 10 == 0 and entity_store.signature() != self._store_sig:
            self.refresh_lists()
        # Poll again fairly soon under load, without starving other Tk events.
        self.after(25 if not self.q.empty() else 100, self._poll)
'''


def _span(lines, start_name, end_name):
    starts = [i for i, line in enumerate(lines) if line == start_name]
    ends = [i for i, line in enumerate(lines) if line == end_name]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise RuntimeError(f"預期的程式定位不唯一：{start_name} / {end_name}；已停止，不修改檔案。")
    return starts[0], ends[0]


def optimize(source):
    src = source.replace("\r\n", "\n")
    if MARKER in src:
        raise RuntimeError("這個 ui.py 已有 GUI_LOG_QUEUE_OPTIMIZATION_V1；不需要重複修補。")
    ast.parse(src)
    lines = src.splitlines(keepends=True)
    if lines.count("import preprocess\n") != 1:
        raise RuntimeError("找不到唯一的 import preprocess；可能是不同版本，請先人工核對。")
    lines.insert(lines.index("import preprocess\n") + 1, "import time\n\n" + CONFIG_BLOCK + "\n")
    queue_start, queue_end = _span(lines, "class QueueWriter:\n", "def make_tree(parent, columns, height=None):\n")
    old_queue = "".join(lines[queue_start:queue_end])
    if 'self.q.put(("log", s))' not in old_queue:
        raise RuntimeError("目前 QueueWriter 與預期版本不同；為避免覆蓋新修改已停止。")
    lines[queue_start:queue_end] = [QUEUE_WRITER_BLOCK + "\n\n"]
    poll_start, poll_end = _span(lines, "    def _poll(self):\n", "    # ================= 文字 =================\n")
    old_poll = "".join(lines[poll_start:poll_end])
    for required in ('self.q.get_nowait()', 'self.log.insert("end", item[1])', 'self.after(100, self._poll)', 'self.refresh_map_view()'):
        if required not in old_poll:
            raise RuntimeError("目前 _poll() 與預期版本不同；為避免覆蓋新修改已停止。缺少：" + required)
    lines[poll_start:poll_end] = [POLL_BLOCK + "\n"]
    result = "".join(lines)
    ast.parse(result)
    compile(result, "ui.py", "exec")
    return result


def main():
    p = argparse.ArgumentParser(description="Conservative GUI log optimization patch for ui.py")
    p.add_argument("--file", default="ui.py", help="Path to your current ui.py")
    p.add_argument("--check", action="store_true", help="Check applicability without touching file")
    a = p.parse_args()
    target = Path(a.file).resolve()
    if not target.is_file():
        p.error(f"找不到檔案：{target}")
    raw = target.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    txt = raw.decode("utf-8-sig")
    out = optimize(txt)
    if a.check:
        print("[PASS] 可以套用 GUI 日誌優化，未修改檔案：", target)
        return
    newline = "\r\n" if b"\r\n" in raw else "\n"
    encoded = out.replace("\n", newline).encode("utf-8")
    if has_bom:
        encoded = b"\xef\xbb\xbf" + encoded
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = target.with_name(f"ui_before_logopt_{timestamp}.py.bak")
    if backup.exists():
        raise RuntimeError("備份路徑已存在：" + str(backup))
    # Preserve original bytes exactly in backup; do not overwrite existing backups.
    with backup.open("xb") as f:
        f.write(raw)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(prefix="ui_logopt_", suffix=".tmp", dir=target.parent, delete=False) as f:
            temp_name = f.name
            f.write(encoded)
        os.replace(temp_name, target)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    print("[PASS] 已更新：", target)
    print("[BACKUP] 原始完整備份：", backup)
    print("[INFO] 僅修改 QueueWriter、_poll 和顯示日誌函式。")


if __name__ == "__main__":
    main()
