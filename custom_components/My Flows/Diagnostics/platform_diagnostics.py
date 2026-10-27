import os
import sys
import json
import time
import socket
import shutil
import platform
import tempfile
import threading
import subprocess
from datetime import datetime, timezone

import requests

from lfx.custom.custom_component.component import Component
from lfx.io import Output
from lfx.schema.message import Message

_LOADED_AT = time.time()
_HISTORY_FILE = os.path.join(tempfile.gettempdir(), "mds_platform_diag_history.json")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _gb(value):
    return str(round(value / (1024 ** 3), 1)) + " GB"


def _meminfo():
    info = {}
    for line in _read("/proc/meminfo").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            info[parts[0].rstrip(":")] = int(parts[1]) * 1024
    return info


def _cgroup_limits():
    notes = []
    cpu_max = _read("/sys/fs/cgroup/cpu.max")
    if cpu_max:
        quota, period = (cpu_max.split() + ["", ""])[:2]
        if quota != "max" and period:
            notes.append("CPU limit (cgroup): " + str(round(int(quota) / int(period), 2)) + " cores")
        else:
            notes.append("CPU limit (cgroup): none")
    quota_v1 = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period_v1 = _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota_v1 and period_v1 and quota_v1 != "-1":
        notes.append("CPU limit (cgroup v1): " + str(round(int(quota_v1) / int(period_v1), 2)) + " cores")
    mem_max = _read("/sys/fs/cgroup/memory.max") or _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if mem_max:
        if mem_max == "max" or int(mem_max) > 1 << 60:
            notes.append("Memory limit (cgroup): none")
        else:
            notes.append("Memory limit (cgroup): " + _gb(int(mem_max)))
    return notes


def _version(package):
    try:
        from importlib.metadata import version
        return version(package)
    except Exception:
        return "not installed"


def _dir_size(path):
    total = 0
    if not os.path.isdir(path):
        return None
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except Exception:
                pass
    return total


class CustomComponent(Component):
    display_name = "Platform Diagnostics"
    description = "Read-only check of the MDS runtime: CPU, memory, GPU, ffmpeg, tzdata, storage, workers and connectivity."

    outputs = [
        Output(display_name="Report", name="diag_report", method="build_diag_report"),
    ]

    def build_diag_report(self) -> Message:
        lines = []
        now = time.time()
        host = socket.gethostname()
        pid = os.getpid()

        lines.append("MDS PLATFORM DIAGNOSTICS - " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
        lines.append("")
        lines.append("== Runtime ==")
        lines.append("Host: " + host + " | PID: " + str(pid))
        lines.append("Python: " + sys.version.split()[0] + " | OS: " + platform.platform())
        lines.append("This process loaded the component " + str(int((now - _LOADED_AT) / 60)) + " min ago")
        lines.append("Active threads: " + str(threading.active_count()))

        lines.append("")
        lines.append("== CPU / Memory ==")
        lines.append("CPU cores visible: " + str(os.cpu_count()))
        try:
            lines.append("Load average (1/5/15 min): " + ", ".join(str(round(x, 2)) for x in os.getloadavg()))
        except Exception:
            pass
        mem = _meminfo()
        if mem:
            lines.append(
                "Memory total: " + _gb(mem.get("MemTotal", 0))
                + " | available: " + _gb(mem.get("MemAvailable", 0))
            )
        lines.extend(_cgroup_limits())

        lines.append("")
        lines.append("== GPU ==")
        if shutil.which("nvidia-smi"):
            try:
                out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=10)
                lines.append("nvidia-smi: " + (out.stdout.strip() or out.stderr.strip() or "no output"))
            except Exception as e:
                lines.append("nvidia-smi error: " + str(e)[:100])
        else:
            lines.append("nvidia-smi: not found")
        try:
            import ctranslate2
            lines.append("CUDA devices (ctranslate2): " + str(ctranslate2.get_cuda_device_count()))
        except Exception as e:
            lines.append("ctranslate2 CUDA check failed: " + str(e)[:100])

        lines.append("")
        lines.append("== Tools / Packages ==")
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            try:
                out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=10)
                lines.append("ffmpeg: " + out.stdout.splitlines()[0])
            except Exception:
                lines.append("ffmpeg: found at " + ffmpeg)
        else:
            lines.append("ffmpeg: NOT installed")
        try:
            from zoneinfo import ZoneInfo
            et = datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M %Z")
            lines.append("Time zones: OK (now " + et + ")")
        except Exception as e:
            lines.append("Time zones: FAILED (" + str(e)[:80] + ") - tzdata likely missing")
        for package in ("faster-whisper", "ctranslate2", "tzdata", "requests", "langflow", "lfx"):
            lines.append("  " + package + ": " + _version(package))

        lines.append("")
        lines.append("== Storage ==")
        tmp = tempfile.gettempdir()
        try:
            usage = shutil.disk_usage(tmp)
            lines.append("Temp dir " + tmp + ": " + _gb(usage.free) + " free of " + _gb(usage.total))
        except Exception:
            lines.append("Temp dir: " + tmp)
        for name in ("qa_transcript_cache", "ceq_dispatcher", "qa_model_benchmark"):
            size = _dir_size(os.path.join(tmp, name))
            lines.append("  " + name + ": " + ("not present" if size is None else _gb(size)))
        hf_home = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
        hf_size = _dir_size(hf_home)
        lines.append("Model cache " + hf_home + ": " + ("not present" if hf_size is None else _gb(hf_size)))
        for var in ("LANGFLOW_CONFIG_DIR", "LANGFLOW_SAVE_DB_IN_CONFIG_DIR", "LANGFLOW_WORKERS"):
            if os.environ.get(var):
                lines.append(var + " = " + os.environ.get(var))
        config_dir = os.environ.get("LANGFLOW_CONFIG_DIR")
        if config_dir:
            lines.append("Config dir writable: " + str(os.access(config_dir, os.W_OK)))

        lines.append("")
        lines.append("== Workers / Persistence (run this 5+ times, minutes apart) ==")
        boot_id = _read("/proc/sys/kernel/random/boot_id")[:8] or "unknown"
        try:
            with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = {"created": now, "runs": []}
        history["runs"].append({"t": now, "host": host, "pid": pid, "boot": boot_id})
        history["runs"] = history["runs"][-50:]
        try:
            with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f)
        except Exception as e:
            lines.append("Could not write history file: " + str(e)[:80])
        hosts = sorted(set(r["host"] for r in history["runs"]))
        pids = sorted(set(str(r["pid"]) for r in history["runs"]))
        boots = sorted(set(r["boot"] for r in history["runs"]))
        lines.append("Runs recorded in this temp dir: " + str(len(history["runs"])))
        lines.append("Temp history file first created: " + str(int((now - history["created"]) / 60)) + " min ago")
        lines.append("Distinct hosts seen: " + str(len(hosts)) + " (" + ", ".join(hosts) + ")")
        lines.append("Distinct processes seen: " + str(len(pids)))
        lines.append("Distinct boots seen: " + str(len(boots)))

        lines.append("")
        lines.append("== Connectivity ==")
        targets = [
            ("Genesys login", "https://login.mypurecloud.com"),
            ("Genesys API", "https://api.mypurecloud.com/api/v2/health/check"),
            ("Matcha API", "https://matcha.harriscomputer.com/rest/api/v1/llms"),
        ]
        for label, url in targets:
            start = time.time()
            try:
                resp = requests.get(url, timeout=8)
                lines.append(
                    "  " + label + ": HTTP " + str(resp.status_code) + " in "
                    + str(int((time.time() - start) * 1000)) + " ms"
                )
            except Exception as e:
                lines.append("  " + label + ": FAILED (" + str(e)[:80] + ")")

        lines.append("")
        lines.append("== How to read the worker section ==")
        lines.append("1 host + 1 process across many runs: single worker; in-memory locks are safe.")
        lines.append("Several processes or hosts: multiple workers; the Dispatcher needs file locks.")
        lines.append("Run count resets to 1 later: temp storage was cleared (restart/redeploy).")
        lines.append("Matcha API 401 is expected here (no key sent); it proves the network path works.")

        return Message(text="\n".join(lines))
