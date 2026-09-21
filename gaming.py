"""Low-overhead game frame telemetry from Intel's PresentMon console application.

No injection, fake FPS, privilege changes, or refresh-rate substitution. The helper
is optional and must have Windows ETW permission. Only runs while Gaming is visible.
"""
from collections import deque
import csv
import ctypes
from ctypes import wintypes
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import psutil


class FrameStats:
    """One rolling second per process/swapchain; never combine multiple swapchains."""
    def __init__(self):
        self.frames = {}

    def add(self, row, now):
        try:
            pid = int(row["ProcessID"])
            ms = float(row.get("msBetweenPresents", row.get("MsBetweenPresents")))
            if not math.isfinite(ms) or not 0 < ms <= 5000:
                return
            key = (pid, row.get("SwapChainAddress", ""))
            if key not in self.frames:
                if len(self.frames) >= 128:
                    self.frames.pop(min(self.frames, key=lambda k: self.frames[k][-1][0]))
                self.frames[key] = deque(maxlen=2000)
            data = self.frames[key]
            data.append((now, ms, row.get("Application", "")))
            while data and data[0][0] < now - 1.0:
                data.popleft()
        except (KeyError, TypeError, ValueError):
            return

    def snapshot(self, pid, now):
        candidates = [d for (p, _), d in self.frames.items() if p == pid and d and now - d[-1][0] < 2]
        if not candidates:
            return None
        data = max(candidates, key=len)
        ms = sum(x[1] for x in data) / len(data)
        return dict(fps=1000 / ms, frame_ms=ms, process=data[-1][2])


def find_presentmon():
    explicit = os.environ.get("BLOB_PRESENTMON")
    if explicit and Path(explicit).is_file():
        return explicit
    bundled = sorted((Path(__file__).parent / "tools").glob("PresentMon*-x64.exe"), reverse=True)
    return str(bundled[0]) if bundled else shutil.which("PresentMon.exe")


class GamingMonitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.stats = FrameStats()
        self.active = threading.Event()
        self.stopping = threading.Event()
        self.proc = None
        self.status = "Open Gaming to start FPS capture."
        self.target_pid = None
        self._retry_at = 0.0
        self.thread = threading.Thread(target=self._run, name="BlobFPS", daemon=True)
        self.thread.start()

    def set_active(self, active):
        if active:
            self.active.set()
        else:
            self.active.clear()

    def _foreground(self):
        user = ctypes.windll.user32
        user.GetForegroundWindow.restype = wintypes.HWND
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(user.GetForegroundWindow(), ctypes.byref(pid))
        if pid.value and pid.value != os.getpid():
            self.target_pid = pid.value

    def snapshot(self):
        self._foreground()
        with self.lock:
            state = self.stats.snapshot(self.target_pid, time.monotonic()) or {}
        memory = psutil.virtual_memory()
        return dict(fps=None, frame_ms=None, **{k: v for k, v in state.items() if k not in ("fps", "frame_ms")},
                    status=self.status, ram_percent=memory.percent, ram_gb=memory.used / 1024**3) | {
                        "fps": state.get("fps"), "frame_ms": state.get("frame_ms")}

    def _read(self, proc):
        header = None
        for line in proc.stdout:
            if self.stopping.is_set() or self.proc is not proc:
                break
            fields = next(csv.reader([line.strip()]), [])
            if "ProcessID" in fields and any(f.lower() == "msbetweenpresents" for f in fields):
                header = fields
            elif header and len(fields) == len(header):
                with self.lock:
                    self.stats.add(dict(zip(header, fields)), time.monotonic())
                self.status = "Focus a game to see its FPS."
            elif "access denied" in line.lower() or "failed to start trace" in line.lower():
                self.status = "Run the installer, then sign out once to enable FPS permission."

    def _start(self):
        path = find_presentmon()
        if not path:
            self.status = "Install PresentMon for FPS and frame time."
            self._retry_at = time.monotonic() + 30
            return
        self.status = "Starting FPS capture…"
        args = [path, "--output_stdout", "--no_console_stats", "--v1_metrics", "--no_track_gpu",
                "--no_track_display", "--no_track_input", "--session_name", f"BlobFPS-{os.getpid()}",
                "--stop_existing_session",
                "--exclude", "dwm.exe", "--exclude", "pythonw.exe", "--exclude", "python.exe"]
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", creationflags=0x08000000)
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _stop(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            # Shut down this helper's ETW session too; killing its process alone can
            # leave a system-wide trace consuming resources after Gaming closes.
            try:
                subprocess.run([proc.args[0], "--session_name", f"BlobFPS-{os.getpid()}",
                                "--terminate_existing_session"], timeout=2,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        with self.lock:
            self.stats = FrameStats()

    def _run(self):
        try:
            while not self.stopping.wait(.25):
                if not self.active.is_set():
                    if self.proc:
                        self._stop()
                    continue
                if self.proc and self.proc.poll() is not None:
                    self._on_exit()
                if self.proc is None and time.monotonic() >= self._retry_at:
                    try:
                        self._start()
                    except OSError:
                        self.status = "Couldn't start PresentMon. Check its installation."
                        self._retry_at = time.monotonic() + 30
        finally:
            self._stop()

    def _on_exit(self):
        self._stop()
        if "permission" not in self.status:
            self.status = "FPS unavailable. Capture stopped; retrying shortly."
        self._retry_at = time.monotonic() + 30

    def shutdown(self):
        self.stopping.set()
        self.thread.join(timeout=3)
