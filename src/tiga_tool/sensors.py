"""Sensor readings for the TIGA stats page.

CPU/GPU temperatures on Apple Silicon come from `macmon` (brew install macmon), which
reads them without sudo. Download speed comes from psutil. Fan RPM is not readable on
Apple Silicon without root, so it is sent as 0 unless --rpm is given.
"""
from __future__ import annotations

import json
import shutil
import statistics
from collections import deque
import subprocess
import threading
import time

import psutil


def _find(obj, *keys):
    """First value found for any of `keys`, searching nested dicts."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and isinstance(obj[k], (int, float)):
                return float(obj[k])
        for v in obj.values():
            r = _find(v, *keys)
            if r is not None:
                return r
    return None


class Smoothed:
    """Median of the last few plausible readings, so one bad sensor sample (a CPU
    "dropping" from 40 to 16 °C for a second) never reaches the screen."""

    def __init__(self, window: int = 5, low: float = 20.0, high: float = 110.0):
        self.values: deque[float] = deque(maxlen=window)
        self.low, self.high = low, high

    def add(self, v: float | None):
        if v is not None and self.low <= v <= self.high:
            self.values.append(v)

    @property
    def value(self) -> float | None:
        return statistics.median(self.values) if self.values else None


class Macmon:
    """Keeps `macmon pipe` running in the background; exposes smoothed temperatures."""

    warmup = 4.0    # seconds to wait for a first plausible sample (the very first is often bogus)

    def __init__(self, interval_ms: int = 1000):
        self.path = shutil.which("macmon")
        self._cpu, self._gpu = Smoothed(), Smoothed()
        self.error: str | None = None if self.path else "macmon not installed (brew install macmon)"
        self._proc = None
        if self.path:
            self._proc = subprocess.Popen([self.path, "pipe"],
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          text=True, bufsize=1)
            threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self._proc.stdout:
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._cpu.add(_find(sample, "cpu_temp_avg", "cpu_temp"))
            self._gpu.add(_find(sample, "gpu_temp_avg", "gpu_temp"))
        self.error = "macmon stopped"

    @property
    def cpu(self) -> float | None:
        return None if self.error else self._cpu.value      # no stale values once macmon stops

    @property
    def gpu(self) -> float | None:
        return None if self.error else self._gpu.value

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


class NetSpeed:
    """Download speed in Mbps across all interfaces except loopback."""

    def __init__(self):
        self._last = (time.monotonic(), self._rx())

    @staticmethod
    def _rx() -> int:
        stats = psutil.net_io_counters(pernic=True)
        return sum(v.bytes_recv for k, v in stats.items() if not k.startswith("lo"))

    def mbps(self) -> float:
        now, rx = time.monotonic(), self._rx()
        t0, rx0 = self._last
        self._last = (now, rx)
        dt = now - t0
        return max(0.0, (rx - rx0) * 8 / 1_000_000 / dt) if dt > 0 else 0.0


class PsutilTemps:
    """Fallback for Linux: CPU from coretemp/k10temp, GPU from amdgpu/nouveau where present."""

    error = None
    warmup = 0.0

    def __init__(self):
        if not hasattr(psutil, "sensors_temperatures"):
            self.error = "no temperature source on this OS (macOS: brew install macmon)"

    @staticmethod
    def _first(groups, names):
        for n in names:
            for t in groups.get(n, []):
                if t.current:
                    return float(t.current)
        return None

    @property
    def cpu(self):
        if self.error:
            return None
        return self._first(psutil.sensors_temperatures(), ("coretemp", "k10temp", "zenpower", "cpu_thermal"))

    @property
    def gpu(self):
        if self.error:
            return None
        return self._first(psutil.sensors_temperatures(), ("amdgpu", "nouveau", "radeon"))

    def stop(self):
        pass


def temperature_source():
    m = Macmon()
    return m if m.path else PsutilTemps()
