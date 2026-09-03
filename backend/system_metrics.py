from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import psutil

def cpu_metrics() -> dict:
    return {
        "usage_percent": round(psutil.cpu_percent(interval=0.15), 1),
        "logical_cores": psutil.cpu_count(logical=True) or os.cpu_count() or 0,
    }


def memory_metrics() -> dict:
    memory = psutil.virtual_memory()
    used = memory.total - memory.available
    return {
        "usage_percent": round(memory.percent, 1),
        "total_gb": round(memory.total / 1024**3, 2),
        "used_gb": round(used / 1024**3, 2),
        "available_gb": round(memory.available / 1024**3, 2),
    }


def disk_metrics() -> dict:
    project_root = Path(__file__).resolve().parents[1]
    usage = psutil.disk_usage(project_root.anchor or project_root)
    return {
        "usage_percent": round(usage.percent, 1),
        "total_gb": round(usage.total / 1024**3, 1),
        "used_gb": round(usage.used / 1024**3, 1),
        "free_gb": round(usage.free / 1024**3, 1),
    }


def _apple_gpu_name() -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=8,
            check=True,
        )
        displays = json.loads(result.stdout).get("SPDisplaysDataType", [])
        return next(
            (str(item.get("sppci_model") or item.get("_name")) for item in displays if item.get("sppci_model") or item.get("_name")),
            None,
        )
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def gpu_metrics() -> dict | None:
    apple_gpu = _apple_gpu_name()
    if apple_gpu:
        # macOS does not expose stable GPU utilization or unified-memory figures here.
        return {
            "name": apple_gpu,
            "utilization_available": False,
            "usage_percent": 0,
            "memory_total_mb": 0,
            "memory_used_mb": 0,
            "memory_usage_percent": 0,
        }

    candidates = [
        shutil.which("nvidia-smi"),
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        r"C:\Windows\System32\nvidia-smi.exe",
    ]
    executable = next((path for path in candidates if path and Path(path).is_file()), None)
    if not executable:
        return None
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=True,
        )
        name, total, used, utilization = [item.strip() for item in result.stdout.splitlines()[0].split(",")]
        return {
            "name": name,
            "utilization_available": True,
            "usage_percent": float(utilization),
            "memory_total_mb": int(total),
            "memory_used_mb": int(used),
            "memory_usage_percent": round(int(used) * 100 / max(int(total), 1), 1),
        }
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        return None


def ollama_metrics() -> dict:
    base_url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    try:
        with urlopen(f"{base_url}/api/ps", timeout=2) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError):
        return {"online": False, "models": []}
    models = []
    for model in data.get("models", []):
        size = int(model.get("size", 0))
        size_vram = int(model.get("size_vram", 0))
        models.append(
            {
                "name": model.get("name", ""),
                "size_gb": round(size / 1024**3, 2),
                "vram_gb": round(size_vram / 1024**3, 2),
                "processor": "GPU" if size and size_vram >= size * 0.9 else "CPU + GPU" if size_vram else "CPU",
            }
        )
    return {"online": True, "models": models}


def machine_metrics() -> dict:
    return {
        "cpu": cpu_metrics(),
        "memory": memory_metrics(),
        "gpu": gpu_metrics(),
        "disk": disk_metrics(),
        "ollama": ollama_metrics(),
        "timestamp": time.time(),
    }
