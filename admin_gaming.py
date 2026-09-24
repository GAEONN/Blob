"""Admin-only Gaming Mode switch for this PC's private Blob build.

Blob never does the switching itself. It starts the existing elevated
``GamingModeToggle`` scheduled task and reads its ``gaming.on`` flag file; the
task's own script shows the result notification.
"""

import os
import subprocess
import threading
import time
from pathlib import Path

TASK_NAME = "GamingModeToggle"
HOME = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents" / "GamingMode"
FLAG = HOME / "gaming.on"
LOG = HOME / "GamingMode.log"
SETTLE_SECONDS = 10.0
NOT_INSTALLED = "Gaming Mode task not installed - ask Claude to set it up"
CREATE_NO_WINDOW = 0x08000000

DESCRIPTION = {
    True: ("Odyssey 2560×1440 · 240 Hz · 125%",
           "iCloud, Apple Music, WhatsApp, Chrome closed",
           "Windows Update paused 12 h · Game Mode on",
           "Wi-Fi power saving off · RAM freed"),
    False: ("Odyssey 1920×1080 · 240 Hz · 100%",
            "Windows Update running normally",
            "iCloud and WhatsApp open",
            "Switch on before launching Fortnite"),
}


def _run_task():
    return subprocess.run(["schtasks.exe", "/run", "/tn", TASK_NAME], capture_output=True,
                          text=True, timeout=15, creationflags=CREATE_NO_WINDOW)


class GamingModeToggle:
    """Thread-safe view of the Gaming Mode task for the UI thread."""

    def __init__(self, flag=FLAG, log=LOG, runner=_run_task, clock=time.monotonic,
                 settle=SETTLE_SECONDS):
        self.flag, self.log, self.runner, self.clock, self.settle = Path(flag), Path(log), runner, clock, settle
        self.on = self.flag.exists()
        self.target = self.on
        self.error = None
        self.busy_until = 0.0
        self.version = 0
        self._seen = -1
        self._next_check = 0.0
        self._lock = threading.Lock()

    @property
    def busy(self):
        return self.clock() < self.busy_until

    def toggle(self):
        """Start the task in the background; ignored while a switch is running."""
        with self._lock:
            if self.busy:
                return False
            self.busy_until = self.clock() + self.settle
            self.target = not self.flag.exists()
            self.error = None
            self.version += 1
        threading.Thread(target=self._trigger, daemon=True).start()
        return True

    def _trigger(self):
        try:
            result = self.runner()
            failed = result.returncode != 0
        except (OSError, subprocess.SubprocessError):
            failed = True
        if failed:
            with self._lock:
                self.error = NOT_INSTALLED
                self.busy_until = 0.0
                self.on = self.flag.exists()
                self.version += 1

    def _log_problem(self):
        try:
            tail = self.log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        except OSError:
            return None
        for line in reversed(tail):
            text = line.strip()
            if text and any(word in text.lower() for word in ("error", "exception", "failed")):
                return "Last run reported a problem: " + text[:140]
        return None

    def poll(self):
        """Cheap per-frame check. Returns True when the card needs a redraw."""
        now = self.clock()
        with self._lock:
            if now >= self._next_check:
                self._next_check = now + 1.0
                settled = self.busy_until and now >= self.busy_until
                on = self.flag.exists()
                if settled:
                    self.busy_until = 0.0
                    self.error = self.error or self._log_problem()
                    self.version += 1
                if on != self.on:
                    self.on = on
                    self.version += 1
            changed = self.version != self._seen
            self._seen = self.version
        return changed

    @property
    def shown_on(self):
        """Where the switch should sit: the target while switching."""
        return self.target if self.busy else self.on

    def status_text(self):
        if self.busy:
            return "Switching…"
        return "On" if self.on else "Off"


def available():
    """Only this PC's build has the Gaming Mode tool folder."""
    return HOME.is_dir()
