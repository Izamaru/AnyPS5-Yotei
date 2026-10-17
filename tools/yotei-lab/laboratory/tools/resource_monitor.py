"""Bounded, read-only Windows resource sampling; no third-party Python dependencies.

Working set and private commit are process metrics. System commit includes shared
sections and other processes. NVIDIA memory is device-wide, never game-only VRAM.
"""
from __future__ import annotations

import csv
import ctypes as ct
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

DWORD = ct.c_uint32
SIZE_T = ct.c_size_t


class PerformanceInfo(ct.Structure):
    _fields_ = [("cb", DWORD)] + [(name, SIZE_T) for name in (
        "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable",
        "SystemCache", "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize"
    )] + [(name, DWORD) for name in ("HandleCount", "ProcessCount", "ThreadCount")]


class ProcessMemory(ct.Structure):
    _fields_ = [("cb", DWORD), ("PageFaultCount", DWORD)] + [(name, SIZE_T) for name in (
        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
        "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
        "PeakPagefileUsage", "PrivateUsage"
    )]


class WindowsSampler:
    def __init__(self, pid):
        if os.name != "nt":
            raise OSError("Windows memory counters unavailable on this platform")
        self.kernel = ct.WinDLL("kernel32", use_last_error=True)
        self.psapi = ct.WinDLL("psapi", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [DWORD, ct.c_int, DWORD]
        self.kernel.OpenProcess.restype = ct.c_void_p
        self.kernel.CloseHandle.argtypes = [ct.c_void_p]
        self.kernel.CloseHandle.restype = ct.c_int
        self.psapi.GetPerformanceInfo.argtypes = [ct.POINTER(PerformanceInfo), DWORD]
        self.psapi.GetPerformanceInfo.restype = ct.c_int
        self.psapi.GetProcessMemoryInfo.argtypes = [ct.c_void_p, ct.POINTER(ProcessMemory), DWORD]
        self.psapi.GetProcessMemoryInfo.restype = ct.c_int
        self.handle = self.kernel.OpenProcess(0x410, False, pid)

    def sample(self):
        result = {}
        system = PerformanceInfo()
        system.cb = ct.sizeof(system)
        if self.psapi.GetPerformanceInfo(ct.byref(system), system.cb):
            page = system.PageSize
            result.update(system_commit_bytes=system.CommitTotal * page,
                          system_commit_limit_bytes=system.CommitLimit * page,
                          system_commit_headroom_bytes=max(0, system.CommitLimit - system.CommitTotal) * page,
                          system_ram_total_bytes=system.PhysicalTotal * page,
                          system_ram_available_bytes=system.PhysicalAvailable * page)
        else:
            result["system_error"] = ct.get_last_error()
        process = ProcessMemory()
        process.cb = ct.sizeof(process)
        if self.handle and self.psapi.GetProcessMemoryInfo(self.handle, ct.byref(process), process.cb):
            result.update(process_working_set_bytes=process.WorkingSetSize,
                          process_peak_working_set_bytes=process.PeakWorkingSetSize,
                          process_private_commit_bytes=process.PrivateUsage)
        else:
            result["process_status"] = "unavailable_or_exited"
        return result

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def parse_gpu_csv(text):
    result = []
    for row in csv.reader(text.splitlines()):
        if len(row) != 4:
            continue
        uuid, name, used, total = (cell.strip() for cell in row)
        def mib(value):
            try:
                number = float(value)
                return int(number * 1048576) if number >= 0 else None
            except (ValueError, OverflowError):
                return None
        result.append({"uuid": uuid, "name": name, "device_used_bytes": mib(used),
                       "device_total_bytes": mib(total)})
    return result


def nvidia_smi_path():
    executable = shutil.which("nvidia-smi")
    if executable:
        return executable
    for path in (Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/nvidia-smi.exe",
                 Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "NVIDIA Corporation/NVSMI/nvidia-smi.exe"):
        if path.is_file():
            return str(path)
    return None


class ResourceMonitor:
    def __init__(self, pid, path, settings, sampler_factory=WindowsSampler, gpu_path=None):
        self.path = Path(path)
        self.stop_event = threading.Event()
        self.started = time.monotonic()
        self.peaks = {}
        self.gpu_peaks = {}
        self.minimum_headroom = None
        self.samples = 0
        self.gpu = {"status": "pending", "scope": "device_total_all_processes"}
        self.gpu_executable = gpu_path or nvidia_smi_path()
        self.file = self.path.open("w", encoding="utf-8")
        self.sampler = None
        self.sampler_error = None
        try:
            self.sampler = sampler_factory(pid)
        except Exception as exc:
            self.sampler_error = str(exc)
        self.write({"type": "metadata", "pid": pid, "settings": settings, "sample_interval_ms": 250,
                    "gpu_interval_ms": 2000, "gpu_scope": "device_total_all_processes",
                    "private_commit_excludes_shared_sections": True,
                    "sampling_can_miss_short_peaks": True, "sampler_error": self.sampler_error})
        self.threads = []

    def write(self, value):
        self.file.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.file.flush()

    def sample(self):
        try:
            values = self.sampler.sample() if self.sampler else {"sampler_error": self.sampler_error}
        except Exception as exc:
            values = {"sampler_error": str(exc)}
        for key in ("process_working_set_bytes", "process_peak_working_set_bytes",
                    "process_private_commit_bytes", "system_commit_bytes"):
            if key in values:
                self.peaks[key] = max(self.peaks.get(key, 0), values[key])
        if "system_commit_headroom_bytes" in values:
            headroom = values["system_commit_headroom_bytes"]
            self.minimum_headroom = headroom if self.minimum_headroom is None else min(self.minimum_headroom, headroom)
        self.samples += 1
        gpu = self.gpu
        for device in gpu.get("devices", []):
            used = device.get("device_used_bytes")
            if used is not None:
                uuid = device["uuid"]
                self.gpu_peaks[uuid] = max(self.gpu_peaks.get(uuid, 0), used)
        self.write({"type": "sample", "unix_time": time.time(), "elapsed_seconds": round(time.monotonic() - self.started, 3),
                    **values, "gpu": gpu})

    def memory_loop(self):
        try:
            while not self.stop_event.wait(0.25):
                self.sample()
        except OSError:
            # Disk full must not terminate the guest or keep retrying the same write.
            self.stop_event.set()

    def gpu_loop(self):
        if not self.gpu_executable:
            self.gpu = {"status": "nvidia_smi_unavailable", "scope": "device_total_all_processes"}
            return
        while not self.stop_event.is_set():
            try:
                process = subprocess.run([self.gpu_executable,
                    "--query-gpu=uuid,name,memory.used,memory.total", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, errors="replace", timeout=2,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                devices = parse_gpu_csv(process.stdout) if process.returncode == 0 else []
                self.gpu = {"status": "ok" if devices else "unavailable", "sample_unix_time": time.time(),
                            "scope": "device_total_all_processes", "devices": devices}
            except (OSError, subprocess.SubprocessError):
                self.gpu = {"status": "query_failed", "scope": "device_total_all_processes"}
                return
            if self.stop_event.wait(2):
                return

    def start(self):
        self.sample()
        for target in (self.memory_loop, self.gpu_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)
        return self

    def close(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join()
        try:
            self.sample()
            self.write({"type": "summary", "samples": self.samples, "sampled_peaks": self.peaks,
                        "device_total_vram_sampled_peaks_bytes": self.gpu_peaks,
                        "minimum_sampled_commit_headroom_bytes": self.minimum_headroom})
        finally:
            if self.sampler:
                self.sampler.close()
            self.file.close()
