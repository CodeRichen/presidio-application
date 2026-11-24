"""V6.6.3 read-only GPU readiness inventory for an offline PII project.

Only collects library versions and GPU driver/provider availability. Does not
load any user documents, create model sessions, change dependencies or paths,
contact the network, or persist interpreter paths, OCR text or error traces.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys

PKGS = (
    "onnxruntime", "onnxruntime-gpu", "onnxruntime-directml",
    "torch", "rapidocr", "rapidocr-onnxruntime", "numpy",
    "nvidia-cuda-runtime-cu12", "nvidia-cudnn-cu12",
    "nvidia-cuda-runtime-cu13", "nvidia-cudnn-cu13",
)


def package_versions():
    versions = {}
    for name in PKGS:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def read_ort():
    try:
        import onnxruntime as ort
        providers = [str(x) for x in ort.get_available_providers()]
        return {"import_ok": True, "version": str(ort.__version__),
                "available_providers": providers,
                "cuda_ep_available": "CUDAExecutionProvider" in providers,
                "cpu_ep_available": "CPUExecutionProvider" in providers}
    except Exception:
        return {"import_ok": False, "version": None,
                "available_providers": [], "cuda_ep_available": False,
                "cpu_ep_available": False}


# A separate interpreter is used so torch DLL imports do not alter this process.
TORCH_PROBE = '''import json
try:
 import torch
 data={"import_ok":True,"version":str(torch.__version__),
       "cuda_compiled_version":str(torch.version.cuda) if torch.version.cuda is not None else None,
       "cuda_device_available":bool(torch.cuda.is_available()),
       "cudnn_version":torch.backends.cudnn.version()}
except Exception:
 data={"import_ok":False,"version":None,"cuda_compiled_version":None,
       "cuda_device_available":False,"cudnn_version":None}
print(json.dumps(data))'''


def read_torch():
    fallback = {"import_ok": False, "version": None,
                "cuda_compiled_version": None, "cuda_device_available": False,
                "cudnn_version": None}
    try:
        run = subprocess.run([sys.executable, "-c", TORCH_PROBE],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True,
                             timeout=45, check=False)
        if run.returncode != 0:
            return fallback
        data = json.loads(run.stdout.strip())
        if not isinstance(data, dict):
            return fallback
        result = {k: data.get(k) for k in fallback}
        result["import_ok"] = bool(result["import_ok"])
        result["cuda_device_available"] = bool(result["cuda_device_available"])
        return result
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return fallback


def parse_nvidia_smi(stdout):
    """Parse non-identifying GPU name/driver/memory; never collect UUIDs/serials."""
    cards = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = [x.strip() for x in line.rsplit(",", 2)]
        if len(fields) != 3:
            continue
        name, driver_version, memory = fields
        cards.append({"name": name[:100], "driver_version": driver_version[:40],
                      "memory_total": memory[:40]})
        if len(cards) == 4:
            break
    return cards


def read_nvidia_smi():
    try:
        run = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
                              "--format=csv,noheader"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True,
                             timeout=12, check=False)
        cards = parse_nvidia_smi(run.stdout) if run.returncode == 0 else []
        return {"available": bool(cards), "gpus": cards}
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "gpus": []}


def classify(ort, pkgs, torch):
    if not ort["import_ok"]:
        return "ORT_IMPORT_FAILED"
    if ort["cuda_ep_available"]:
        return "CUDA_EP_LISTED_NOT_EXECUTION_PROOF"
    if pkgs.get("onnxruntime") and pkgs.get("onnxruntime-gpu"):
        return "CUDA_EP_MISSING_MULTIPLE_ORT_DISTS"
    if pkgs.get("onnxruntime-gpu"):
        return "CUDA_EP_MISSING_WITH_GPU_DIST"
    if pkgs.get("onnxruntime"):
        return "CUDA_EP_MISSING_CPU_DIST_PRESENT"
    return "CUDA_EP_MISSING_UNKNOWN_DIST"


def inventory():
    pkgs = package_versions()
    ort = read_ort()
    torch = read_torch()
    nvidia = read_nvidia_smi()
    return {
        "schema": "pii_v663_gpu_readiness_1",
        "status": classify(ort, pkgs, torch),
        "read_only": True,
        "documents_accessed": False,
        "dependencies_modified": False,
        "python": {"version": ".".join(map(str, sys.version_info[:3])),
                   "bits": struct.calcsize("P") * 8, "os": platform.system()},
        "package_versions": pkgs,
        "onnxruntime": ort,
        "pytorch": torch,
        "nvidia_smi": nvidia,
        "cuda_toolkit_path_present": bool(os.environ.get("CUDA_PATH")),
        "note": "CUDA provider listing is not proof of GPU kernel execution. No OCR model was run."
    }


def report_markdown(data):
    ort = data["onnxruntime"]
    torch = data["pytorch"]
    driver = data["nvidia_smi"]
    ver = data["package_versions"]
    value = lambda x: str(x) if x is not None else "未安裝或未知"
    cards = driver["gpus"]
    lines = [
        "# V6.6.3：OCR GPU 環境唯讀檢查", "",
        f"**狀態：{data['status']}**", "",
        "本工具只讀取套件版本與 GPU Provider，未讀取任何 PDF、OCR 內容或私人對照表，亦未安裝任何套件。", "",
        "| 項目 | 結果 |", "|---|---|",
        f"| Python | {data['python']['version']} ({data['python']['bits']}-bit) |",
        f"| 作業系統 | {data['python']['os']} |",
        f"| onnxruntime | {value(ver.get('onnxruntime'))} |",
        f"| onnxruntime-gpu | {value(ver.get('onnxruntime-gpu'))} |",
        f"| ORT 匯入 | {ort['import_ok']} |",
        f"| CUDA Execution Provider 可列出 | {ort['cuda_ep_available']} |",
        f"| CPU Execution Provider 可列出 | {ort['cpu_ep_available']} |",
        f"| PyTorch | {value(torch['version'])} |",
        f"| PyTorch CUDA 編譯版本 | {value(torch['cuda_compiled_version'])} |",
        f"| PyTorch CUDA 裝置可用 | {torch['cuda_device_available']} |",
        f"| PyTorch cuDNN 版本 | {value(torch['cudnn_version'])} |",
        f"| nvidia-smi 可用 | {driver['available']} |",
        f"| 系統 CUDA_PATH 是否設定 | {data['cuda_toolkit_path_present']} |",
        "", "## GPU／驅動（不含序號與 UUID）", ""
    ]
    if cards:
        for i, card in enumerate(cards, 1):
            lines.append(f"- GPU {i}: {card['name']}；驅動 {card['driver_version']}；顯示記憶體 {card['memory_total']}")
    else:
        lines.append("nvidia-smi 未提供可用 GPU 資訊。")
    lines += ["", "## 已安裝 GPU 相關套件", ""]
    for name in ("nvidia-cuda-runtime-cu12", "nvidia-cudnn-cu12",
                 "nvidia-cuda-runtime-cu13", "nvidia-cudnn-cu13", "onnxruntime-directml"):
        lines.append(f"- {name}: {value(ver.get(name))}")
    lines += ["", "注意：`CUDAExecutionProvider` 出現在清單中，不等於 OCR 確實執行 CUDA kernel。",
              "請先回傳本報告，不要直接修改正式 `.venv`。"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="唯讀的 Windows OCR GPU 能力盤點；不安裝、不接觸文件")
    parser.add_argument("--results-dir", default="v663_gpu_readiness_results")
    parser.add_argument("--print-only", action="store_true", help="只在終端顯示 JSON，不寫檔案")
    args = parser.parse_args(argv)
    result = inventory()
    if args.print_only:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        target = Path(args.results_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "v663_gpu_readiness_summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (target / "v663_gpu_readiness_summary.md").write_text(
            report_markdown(result), encoding="utf-8")
        print("V6.6.3 STATUS:", result["status"])
        print("REPORTS:", target / "v663_gpu_readiness_summary.json")
        print("       ", target / "v663_gpu_readiness_summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
