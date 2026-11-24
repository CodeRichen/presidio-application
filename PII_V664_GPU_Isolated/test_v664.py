"""Unit tests that do not require CUDA or GPU packages."""
import tempfile
import unittest
from pathlib import Path
from v664_gpu_smoke import evaluate, _write_result


class V664Tests(unittest.TestCase):
    def test_cuda_absent(self):
        self.assertEqual(evaluate(False, 0, False), "CUDA_EP_NOT_AVAILABLE")

    def test_zero_cuda_events(self):
        self.assertEqual(evaluate(True, 0, True), "NO_CONFIRMED_CUDA_KERNEL")

    def test_mismatch(self):
        self.assertEqual(evaluate(True, 2, False), "OUTPUT_MISMATCH")

    def test_verified(self):
        self.assertEqual(evaluate(True, 2, True), "GPU_KERNEL_VERIFIED_SYNTHETIC_ONLY")

    def test_error(self):
        self.assertEqual(evaluate(True, 2, True, "ImportError"), "RUNTIME_ERROR")

    def test_reports_safe(self):
        with tempfile.TemporaryDirectory() as d:
            _write_result(Path(d), {"status":"CUDA_EP_NOT_AVAILABLE", "documents_accessed":False})
            self.assertTrue((Path(d)/"v664_gpu_smoke_summary.json").exists())
            self.assertTrue((Path(d)/"v664_gpu_smoke_summary.md").exists())
            self.assertNotIn("ocr_text", (Path(d)/"v664_gpu_smoke_summary.json").read_text())


if __name__ == "__main__":
    unittest.main()
