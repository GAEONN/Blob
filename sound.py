"""Sound controller (UI side): presets, output devices, default-device routing, and the
audio_engine.py process it drives through shared memory.

The engine process is started warm when the app launches, so turning the effect on is instant:
it only re-routes Windows' default output (on a worker thread — COM calls never block the UI)."""
import os
import queue
import subprocess
import sys
import threading
import time
from multiprocessing import shared_memory

import numpy as np
import psutil

import engine as core
from audio_engine import (BASS, BOOST, CLARITY, ENABLED, EQ0, HEARTBEAT, LEVEL, N_BANDS, QUIT, SHM_NAME,
                          SPEC0, STATUS, SURROUND, VERSION)

HERE = os.path.dirname(os.path.abspath(__file__))
CABLE_IN = "CABLE Input (VB-Audio Virtual Cable)"
VIRTUAL = ("CABLE", "VB-Audio", "FxSound")

PRESETS = {
    #            31   62  125  250  500   1k   2k   4k   8k  16k   surround
    "music":  ([3.0, 2.5, 1.5, 0.0, -0.5, 0.0, 0.5, 1.5, 2.5, 3.0], 0.25),
    "movies": ([2.5, 2.0, 1.0, 0.0, 0.0, 1.0, 2.0, 2.5, 1.5, 1.0], 0.55),
    "gaming": ([1.0, 1.0, 0.0, -1.0, 0.0, 1.5, 3.0, 3.5, 2.5, 1.0], 0.40),
    "voice":  ([-4.0, -3.0, -1.0, 0.0, 1.0, 2.5, 3.0, 2.0, 0.0, -1.0], 0.0),
    "flat":   ([0.0] * 10, 0.0),
}
PRESET_ORDER = [("music", "Music"), ("movies", "Movies"), ("gaming", "Gaming"), ("voice", "Voice"),
                ("flat", "Flat")]


# ─────────────────────────── Windows audio endpoints (worker thread only) ───────────────────────────
# Enumerating endpoints is slow (~300 ms: every property of every device is read), so the active
# playback devices are enumerated once and cached; volume/default calls then take ~1 ms.
_cache = {}


def _devices(refresh=False):
    if refresh or not _cache:
        from pycaw.pycaw import AudioUtilities
        _cache.clear()
        for d in AudioUtilities.GetAllDevices(data_flow=0, device_state=1):
            if d.FriendlyName:
                _cache[d.FriendlyName] = d
    return _cache


def _dev(name):
    d = _devices().get(name)
    if d is None:
        d = _devices(refresh=True).get(name)
    return d


def output_devices(refresh=False):
    """[(name, endpoint_id)] of real, active playback devices."""
    try:
        return [(n, d.id) for n, d in _devices(refresh).items() if not any(v in n for v in VIRTUAL)]
    except Exception:
        return []


def device_id(name):
    d = _dev(name) if name else None
    return d.id if d else None


def default_output_name():
    try:
        from pycaw.pycaw import AudioUtilities
        dev = AudioUtilities.GetDeviceEnumerator().GetDefaultAudioEndpoint(0, 1)  # eRender, eMultimedia
        did = dev.GetId()
        return next((n for n, d in _devices().items() if d.id == did), None) or             next((n for n, d in _devices(refresh=True).items() if d.id == did), "")
    except Exception:
        return ""


def set_default(endpoint_id):
    from pycaw.pycaw import AudioUtilities
    from pycaw.constants import ERole
    AudioUtilities.SetDefaultDevice(endpoint_id, [ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])


_vol = {}


def _endpoint_volume(name):
    if name not in _vol:
        _vol[name] = _dev(name).EndpointVolume
    return _vol[name]


def get_volume(name):
    try:
        return _endpoint_volume(name).GetMasterVolumeLevelScalar()
    except Exception:
        _vol.pop(name, None)
        return None


def set_volume(name, scalar):
    try:
        _endpoint_volume(name).SetMasterVolumeLevelScalar(float(scalar), None)
    except Exception:
        _vol.pop(name, None)


def fxsound_procs():
    return [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower().startswith("fxsound")]


def fxsound_running():
    return bool(fxsound_procs())


class Sound:
    def __init__(self):
        self.cfg = core.load_config().get("sound", {})
        self.boost = self.cfg.get("boost", 0.5)
        self.bass = self.cfg.get("bass", 0.3)
        self.clarity = self.cfg.get("clarity", 0.3)
        self.surround = self.cfg.get("surround", PRESETS["music"][1])
        self.preset = self.cfg.get("preset", "music")
        self.output = self.cfg.get("output")
        self.enabled = False       # what the UI shows (flips instantly)
        self.routed = False        # what Windows is actually doing
        self.proc = None
        self.error = ""
        self.fx_conflict = False
        self.volume = None         # Windows master volume (whatever device is default), 0..1
        self.spectrum = np.zeros(N_BANDS)
        try:
            self.shm = shared_memory.SharedMemory(name=SHM_NAME, create=True, size=64 * 8)
        except FileExistsError:
            self.shm = shared_memory.SharedMemory(name=SHM_NAME)
        self.p = np.ndarray((64,), np.float64, buffer=self.shm.buf)
        self.p[:] = 0
        self.p[HEARTBEAT] = time.time()
        self.push()
        self.jobs = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()
        self.jobs.put(("warm", None))
        opts = core.load_config().get("options", {})
        if self.cfg.get("enabled") and opts.get("soundstart", False):
            self.set_enabled(True)

    # ── settings ──
    def save(self):
        cfg = core.load_config()
        cfg["sound"] = {"boost": self.boost, "bass": self.bass, "clarity": self.clarity, "surround": self.surround,
                        "preset": self.preset, "output": self.output, "enabled": self.enabled}
        core.save_config(cfg)

    def push(self):
        p = self.p
        p[BOOST], p[BASS], p[CLARITY], p[SURROUND] = self.boost, self.bass, self.clarity, self.surround
        p[EQ0:EQ0 + 10] = PRESETS[self.preset][0]
        p[ENABLED] = 1.0
        p[VERSION] += 1

    def set(self, key, value):
        setattr(self, key, max(0.0, min(1.0, value)))
        self.push()

    def set_preset(self, name):
        self.preset = name
        self.surround = PRESETS[name][1]
        self.push()
        self.save()

    @property
    def boost_db(self):
        return 15.0 * self.boost

    # ── UI-facing actions (all instant; the work happens on the worker) ──
    def set_enabled(self, on):
        self.error = ""
        if on and fxsound_running():
            self.fx_conflict = True
            self.error = "FxSound is running and keeps taking the audio."
            return
        self.fx_conflict = False
        self.enabled = on
        self.save()
        self.jobs.put(("route", on))

    def next_output(self):
        self.jobs.put(("next_output", None))

    def set_system_volume(self, v):
        self.volume = max(0.0, min(1.0, v))
        self.jobs.put(("volume", self.volume))

    def refresh_devices(self):
        self.jobs.put(("refresh", None))

    def quit_fxsound(self):
        for proc in fxsound_procs():
            try:
                proc.terminate()
            except psutil.Error:
                pass
        psutil.wait_procs(fxsound_procs(), timeout=3)
        self.fx_conflict = False
        self.error = ""
        self.set_enabled(True)

    def heartbeat(self):
        self.p[HEARTBEAT] = time.time()
        if self.routed or self.enabled:
            new = self.p[SPEC0:SPEC0 + N_BANDS]
            self.spectrum = np.maximum(new, self.spectrum * 0.82)
        else:
            self.spectrum *= 0.8

    def level(self):
        return float(self.p[LEVEL])

    def shutdown(self):
        done = threading.Event()
        self.jobs.put(("shutdown", done))
        done.wait(5)
        self.p = None
        self.shm.close()
        try:
            self.shm.unlink()
        except Exception:
            pass

    # ── worker thread: engine process + Windows routing ──
    def _worker(self):
        import comtypes
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass  # COM already initialised on this thread
        last_watch = 0.0
        while True:
            try:
                job, arg = self.jobs.get(timeout=0.5)
            except queue.Empty:
                job, arg = None, None
            try:
                if job == "warm":
                    self._pick_output()
                    self._ensure_engine()
                elif job == "route":
                    self._route(arg)
                elif job == "refresh":
                    self._pick_output()
                elif job == "next_output":
                    self._next_output()
                elif job == "volume":
                    while not self.jobs.empty():  # only the latest value matters while dragging
                        nxt = self.jobs.queue[0]
                        if nxt[0] != "volume":
                            break
                        arg = self.jobs.get_nowait()[1]
                    set_volume(default_output_name(), arg)
                elif job == "shutdown":
                    if self.routed:
                        self._unroute()
                    self._stop_engine()
                    arg.set()
                    return
                if time.time() - last_watch > 1.5:
                    last_watch = time.time()
                    self._watch()
            except Exception as e:
                core.log(f"sound worker: {job}: {e!r}")

    def _pick_output(self):
        names = [n for n, _ in output_devices(refresh=True)]
        if self.output not in names:
            current = default_output_name()
            self.output = current if current in names else (names[0] if names else None)

    def _ensure_engine(self):
        if self.proc and self.proc.poll() is None and self.p[STATUS] == 1:
            return True
        self._stop_engine()
        if not self.output:
            self._pick_output()
        out_id = device_id(self.output) if self.output else None
        if not out_id:
            self.error = "No output device found."
            return False
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        self.p[QUIT] = 0
        self.p[STATUS] = 0
        self.proc = subprocess.Popen([pythonw, os.path.join(HERE, "audio_engine.py"), self.output, out_id],
                                     creationflags=0x08000000)
        for _ in range(80):  # wait up to 8 s for the stream to open (only at launch / device change)
            time.sleep(0.1)
            if self.p[STATUS] != 0 or self.proc.poll() is not None:
                break
        if self.p[STATUS] != 1:
            self._stop_engine()
            self.error = "Couldn't open the audio device."
            return False
        return True

    def _stop_engine(self):
        if self.proc and self.proc.poll() is None:
            self.p[QUIT] = 1
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def _route(self, on):
        if on and not self.routed:
            if not self._ensure_engine():
                self.enabled = False
                return
            cable = device_id(CABLE_IN)
            if not cable:
                self.error = "VB-Audio Virtual Cable is missing."
                self.enabled = False
                return
            # like FxSound: the real device runs at 100 % and your volume slider moves to the cable
            vol = get_volume(self.output)
            if vol is not None:
                set_volume(CABLE_IN, vol)
                set_volume(self.output, 1.0)
            set_default(cable)
            self.routed = True
        elif not on and self.routed:
            self._unroute()

    def _unroute(self):
        vol = get_volume(CABLE_IN)
        if vol is not None and self.output:
            set_volume(self.output, vol)
        out_id = device_id(self.output) if self.output else None
        if out_id:
            set_default(out_id)
        self.routed = False

    def _next_output(self):
        names = [n for n, _ in output_devices(refresh=True)]
        if not names:
            return
        new = names[(names.index(self.output) + 1) % len(names)] if self.output in names else names[0]
        self._switch_output(new)

    def _switch_output(self, new):
        was = self.routed
        if was:
            self._unroute()
        self.output = new
        self.save()
        self._stop_engine()
        self._ensure_engine()
        if was:
            self._route(True)

    def _watch(self):
        """Follow the user: if Windows' default output was moved to another real device while we're
        on, play there instead. Also restart the engine if it died."""
        v = get_volume(default_output_name())
        if v is not None:
            self.volume = v
        if self.proc and self.proc.poll() is not None:
            self.proc = None
            if self.enabled:
                self.routed = False
                self._route(True)
            else:
                self._ensure_engine()
        if not self.routed:
            return
        current = default_output_name()
        if not current or current == CABLE_IN:
            return
        if any(v in current for v in VIRTUAL):
            if fxsound_running():
                self.fx_conflict = True
                self.error = "FxSound took over the audio again."
            return
        self.routed = False  # Windows is no longer pointing at us
        set_volume(self.output, get_volume(CABLE_IN) or 1.0)
        self.output = current
        self.save()
        self._stop_engine()
        self._ensure_engine()
        self._route(True)
