# -*- coding: utf-8 -*-
"""Headless tests: no Tk display / Windows packages / AI model required."""
import ast
import queue
import threading
from pathlib import Path
import types
import unittest
import time

from apply_ui_optimization import optimize, MARKER

HERE = Path(__file__).resolve().parent
REFERENCE = HERE / "ui_original_reference.py"
OPTIMIZED = HERE / "ui_optimized_reference.py"


def extract_test_parts(text):
    tree = ast.parse(text)
    writer = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "QueueWriter")
    original_app = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "App")
    methods = [node for node in original_app.body if isinstance(node, ast.FunctionDef)
               and node.name in {"_poll", "_append_log_batch"}]
    assert len(methods) == 2
    app = ast.ClassDef(name="IsolatedApp", bases=[], keywords=[], body=methods, decorator_list=[])
    mini = ast.fix_missing_locations(ast.Module(body=[writer, app], type_ignores=[]))
    env = {"threading":threading,"queue":queue,"time":time,
           "entity_store": types.SimpleNamespace(signature=lambda: "stable"),
           "messagebox": types.SimpleNamespace(showerror=lambda *_: None),
           "GUI_LOG_POLL_MAX_ITEMS":250,"GUI_LOG_POLL_BUDGET_MS":100,
           "GUI_LOG_QUEUE_LIMIT":12000,"GUI_LOG_MAX_RECORD_CHARS":16000,
           "GUI_LOG_MAX_LINES":3000}
    exec(compile(mini, "isolated_ui_test", "exec"), env)
    return env


class FakeLog:
    def __init__(self):
        self.text = ""
        self.insert_calls = 0
        self.states = []
        self.delete_calls = 0

    def configure(self, state): self.states.append(state)
    def insert(self, _where, s):
        self.insert_calls += 1
        self.text += s
    def index(self, _where):
        return f"{self.text.count(chr(10))+1}.0"
    def delete(self, begin, end):
        n = int(end.split(".")[0]) - 1
        self.text = "".join(self.text.splitlines(keepends=True)[n:])
        self.delete_calls += 1
    def see(self, _): pass

class FakeApp:
    def __init__(self, app_class, env):
        self.__class__ = type("FakeAppWithMethods", (FakeApp, app_class), {})
        self.q = queue.Queue()
        self.log = FakeLog()
        self.mapping = {}
        self._map_n = 0
        self._tick = 0
        self._store_sig = "stable"
        self.busy = True
        self.status = types.SimpleNamespace(set=lambda s: None)
        self._on_done = None
        self.after_delays = []
        self.callbacks = []
        self.errors = []
        self.map_refreshes = 0

    def _set_busy(self, v): self.busy = v
    def refresh_map_view(self): self.map_refreshes += 1
    def refresh_lists(self): pass
    def after(self, n, fn): self.after_delays.append(n)

class GuiLogOptimizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = REFERENCE.read_text(encoding="utf-8-sig")
        cls.after = OPTIMIZED.read_text(encoding="utf-8-sig")
        cls.env = extract_test_parts(cls.after)

    def test_compiles_and_preserves_other_functions(self):
        before = ast.parse(self.before)
        after = ast.parse(self.after)
        def funcs(tree):
            app = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "App")
            return {x.name:ast.dump(x, include_attributes=False) for x in app.body if isinstance(x,ast.FunctionDef)}
        a,b=funcs(before),funcs(after)
        self.assertIn("_append_log_batch",b)
        self.assertEqual(set(a)-{"_poll"},set(b)-{"_poll","_append_log_batch"})
        for fn in set(a)-{"_poll"}: self.assertEqual(a[fn],b[fn],fn)
        self.assertIn(MARKER,self.after)

    def test_patcher_idempotence_and_safety(self):
        self.assertEqual(optimize(self.before).replace("\r\n","\n"),self.after.replace("\r\n","\n"))
        with self.assertRaises(RuntimeError): optimize(self.after)
        with self.assertRaises(RuntimeError): optimize(self.before.replace('self.log.insert("end", item[1])','self.log.insert("end", item[9])'))

    def test_batched_updates_are_bounded_and_done_delivered(self):
        self.env["GUI_LOG_POLL_MAX_ITEMS"] = 100
        a=FakeApp(self.env["IsolatedApp"],self.env)
        a._on_done = lambda x: a.callbacks.append(x)
        for i in range(399): a.q.put(("log", f"line{i}\n"))
        a.q.put(("done", "done-value", None))
        a._poll()
        self.assertEqual(a.log.insert_calls,1)
        self.assertTrue(a.busy)
        self.assertEqual(a.after_delays, [25])
        for _ in range(3): a._poll()
        self.assertEqual(a.callbacks,["done-value"])
        self.assertFalse(a.busy)
        self.assertEqual(a.log.insert_calls,4)
        self.assertEqual(a.q.qsize(),0)

    def test_bounded_widget_history(self):
        self.env["GUI_LOG_MAX_LINES"] = 4
        a=FakeApp(self.env["IsolatedApp"],self.env)
        a._append_log_batch(["one\ntwo\nthree\nfour\nfive\n"])
        self.assertGreaterEqual(a.log.delete_calls,1)
        self.assertTrue(a.log.text.endswith("five\n"))
        self.assertNotIn("one\n",a.log.text)
        self.assertEqual(a.log.states[-1],"disabled")

    def test_high_water_keeps_error_and_reports_drops(self):
        self.env["GUI_LOG_QUEUE_LIMIT"] = 3
        q=queue.Queue()
        w=self.env["QueueWriter"](q)
        for x in range(15): w.write(f"spam-{x}\n")
        self.assertLess(q.qsize(),15)
        self.assertEqual(w._dropped,12)
        w.write("[錯誤] failed safely\n")
        vals=[q.get_nowait()[1] for _ in range(q.qsize())]
        self.assertTrue(any("省略 12 段" in x for x in vals))
        self.assertTrue(any("[錯誤]" in x for x in vals))
        self.assertEqual(w._dropped,0)

    def test_oversized_gui_log_record_clipped(self):
        self.env["GUI_LOG_MAX_RECORD_CHARS"] = 8
        q=queue.Queue()
        w=self.env["QueueWriter"](q)
        w.write("abcdefghijklmno")
        val=q.get_nowait()[1]
        self.assertTrue(val.startswith("abcdefgh"))
        self.assertIn("省略",val)

    def test_error_result_restores_busy_state_and_next_success(self):
        a=FakeApp(self.env["IsolatedApp"],self.env)
        errors=[]
        self.env["messagebox"] = types.SimpleNamespace(showerror=lambda _,v:errors.append(v))
        a.q.put(("done", None, RuntimeError("synthetic error")))
        a._poll()
        self.assertFalse(a.busy)
        self.assertEqual(errors,["synthetic error"])
        a.busy=True
        a._on_done=lambda x:a.callbacks.append(x)
        a.q.put(("done", 42, None))
        a._poll()
        self.assertEqual(a.callbacks,[42])

if __name__ == "__main__":
    unittest.main(verbosity=2)
