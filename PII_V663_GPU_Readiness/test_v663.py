"""Unit tests for V6.6.3 (use only synthetic data; no package installation)."""
import json
import unittest
from unittest.mock import patch
import v663_gpu_readiness as d


class TestV663(unittest.TestCase):
    def setUp(self):
        self.pkgs = {k: None for k in d.PKGS}
        self.ort = {'import_ok': True, 'cuda_ep_available': False}
        self.torch = {'import_ok': True, 'cuda_device_available': True}

    def test_ort_cpu_only(self):
        self.pkgs['onnxruntime'] = '1.22.0'
        self.assertEqual(d.classify(self.ort, self.pkgs, self.torch), 'CUDA_EP_MISSING_CPU_DIST_PRESENT')

    def test_both_dists(self):
        self.pkgs['onnxruntime'] = '1.22.0'
        self.pkgs['onnxruntime-gpu'] = '1.22.0'
        self.assertEqual(d.classify(self.ort, self.pkgs, self.torch), 'CUDA_EP_MISSING_MULTIPLE_ORT_DISTS')

    def test_gpu_dist_but_missing_ep(self):
        self.pkgs['onnxruntime-gpu'] = '1.22.0'
        self.assertEqual(d.classify(self.ort, self.pkgs, self.torch), 'CUDA_EP_MISSING_WITH_GPU_DIST')

    def test_missing_ort(self):
        self.ort['import_ok'] = False
        self.assertEqual(d.classify(self.ort, self.pkgs, self.torch), 'ORT_IMPORT_FAILED')

    def test_cuda_listed_not_proof(self):
        self.ort['cuda_ep_available'] = True
        self.assertEqual(d.classify(self.ort, self.pkgs, self.torch), 'CUDA_EP_LISTED_NOT_EXECUTION_PROOF')

    def test_unknown_dist(self):
        self.assertEqual(d.classify(self.ort, self.pkgs, self.torch), 'CUDA_EP_MISSING_UNKNOWN_DIST')

    def test_nvidia_parse(self):
        cards = d.parse_nvidia_smi('NVIDIA RTX 4060 Laptop GPU, 560.94, 8188 MiB\n')
        self.assertEqual(cards[0]['driver_version'], '560.94')
        self.assertEqual(cards[0]['name'], 'NVIDIA RTX 4060 Laptop GPU')
        self.assertEqual(cards[0]['memory_total'], '8188 MiB')

    def test_nvidia_empty(self):
        self.assertEqual(d.parse_nvidia_smi(''), [])
        self.assertEqual(d.parse_nvidia_smi('unexpected output'), [])

    def test_nvidia_limit(self):
        cards = d.parse_nvidia_smi('GPU, 500, 8 MiB\n' * 12)
        self.assertEqual(len(cards), 4)

    def test_report_redaction(self):
        fake = {'status':'CUDA_EP_MISSING_CPU_DIST_PRESENT',
                'python': {'version':'3.11.18', 'bits':64, 'os':'Windows'},
                'package_versions': self.pkgs,
                'onnxruntime': {'import_ok':True, 'cuda_ep_available':False, 'cpu_ep_available':True},
                'pytorch':{'version':'2.11', 'cuda_compiled_version':'12.8',
                           'cuda_device_available':True,'cudnn_version':90000},
                'nvidia_smi':{'available':False, 'gpus':[]},
                'cuda_toolkit_path_present':False}
        doc = d.report_markdown(fake)
        self.assertIn('CUDA_EP_MISSING_CPU_DIST_PRESENT', doc)
        self.assertNotIn('C:\\Users', doc)

    def test_inventory_no_documents(self):
        with patch.object(d,'package_versions',return_value=self.pkgs),\
             patch.object(d,'read_ort',return_value=self.ort),\
             patch.object(d,'read_torch',return_value=self.torch),\
             patch.object(d,'read_nvidia_smi',return_value={'available':False,'gpus':[]}):
            res=d.inventory()
        self.assertFalse(res['documents_accessed'])
        self.assertFalse(res['dependencies_modified'])
        self.assertTrue(res['read_only'])
        json.dumps(res)


if __name__ == '__main__':
    unittest.main(verbosity=2)
