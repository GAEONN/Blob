"""Blob sound engine — runs as its own process so audio never stutters because of the UI.

Windows plays everything into the VB-Audio virtual cable; we read it back from "CABLE Output",
run EQ → bass → clarity → surround → boost → loudness compressor → look-ahead limiter, and
play it on the real output device (held at 100 %; your volume slider lives on the cable).
VB-CABLE reports hardware volume, so Windows never attenuates what goes through it and the cable
itself ignores its own slider and mute. The app mirrors them into VOLUME and we apply them here.
Parameters and the spectrum are exchanged through shared memory.

usage: pythonw audio_engine.py "<output device name>" "<output endpoint id>" "<shared memory name>"
"""
import math
import os
import sys
import time
from multiprocessing import shared_memory

# numpy/scipy's OpenBLAS otherwise spawns a thread per CPU and reserves ~1.5 GB; our math is tiny
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import numpy as np
import sounddevice as sd
from scipy.signal import sosfilt

SHM_NAME = "BlobSound"
# shared-memory slots (float64)
HEARTBEAT, ENABLED, BOOST, BASS, CLARITY, SURROUND, VERSION, STATUS, QUIT, LEVEL = range(10)
VOLUME = 12   # linear gain from the cable's Windows volume and mute; the app writes it
EQ0, SPEC0 = 16, 32
EQ_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
N_BANDS = 28
RATE, BLOCK = 48000, 480
CEILING = 10 ** (-0.5 / 20)
MAX_BOOST_DB = 15.0


# ─────────────────────────── filter design (RBJ cookbook) ───────────────────────────
def _norm(b, a):
    return [b[0] / a[0], b[1] / a[0], b[2] / a[0], 1.0, a[1] / a[0], a[2] / a[0]]


def peaking(f, gain_db, q):
    A, w = 10 ** (gain_db / 40), 2 * math.pi * f / RATE
    al = math.sin(w) / (2 * q)
    return _norm([1 + al * A, -2 * math.cos(w), 1 - al * A], [1 + al / A, -2 * math.cos(w), 1 - al / A])


def shelf(f, gain_db, high, s=0.8):
    A, w = 10 ** (gain_db / 40), 2 * math.pi * f / RATE
    al = math.sin(w) / 2 * math.sqrt((A + 1 / A) * (1 / s - 1) + 2)
    c, sa = math.cos(w), 2 * math.sqrt(A) * al
    if high:
        b = [A * ((A + 1) + (A - 1) * c + sa), -2 * A * ((A - 1) + (A + 1) * c), A * ((A + 1) + (A - 1) * c - sa)]
        a = [(A + 1) - (A - 1) * c + sa, 2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - sa]
    else:
        b = [A * ((A + 1) - (A - 1) * c + sa), 2 * A * ((A - 1) - (A + 1) * c), A * ((A + 1) - (A - 1) * c - sa)]
        a = [(A + 1) + (A - 1) * c + sa, -2 * ((A - 1) + (A + 1) * c), (A + 1) + (A - 1) * c - sa]
    return _norm(b, a)


def design(p):
    """Fixed number of sections so filter state carries over smoothly when settings change."""
    sections = [peaking(f, float(p[EQ0 + i]), 1.1) for i, f in enumerate(EQ_FREQS)]
    sections.append(shelf(95, 12 * float(p[BASS]), high=False))             # bass boost
    sections.append(shelf(4500, 8 * float(p[CLARITY]), high=True))          # clarity / air
    sections.append(peaking(2800, 3 * float(p[CLARITY]), 0.9))               # presence
    return np.array(sections, np.float64)


# ─────────────────────────── engine ───────────────────────────
class Engine:
    def __init__(self, shm):
        self.p = np.ndarray((64,), np.float64, buffer=shm.buf)
        self.version = -1
        self.sos = design(self.p)
        self.zi = np.zeros((len(self.sos), 2, 2))
        self.delay = np.zeros((BLOCK, 2), np.float32)  # one block of look-ahead for the limiter
        self.gain = 1.0
        self.volume = 1.0
        self.comp_db = 0.0
        self.ring = np.zeros(2048, np.float32)
        self.blocks = 0
        edges = np.geomspace(40, 16000, N_BANDS + 1)
        freqs = np.fft.rfftfreq(2048, 1 / RATE)
        self.band_idx = [np.where((freqs >= edges[i]) & (freqs < edges[i + 1]))[0] for i in range(N_BANDS)]
        self.band_idx = [ix if len(ix) else np.array([np.argmin(abs(freqs - edges[i]))])
                         for i, ix in enumerate(self.band_idx)]
        self.window = np.hanning(2048).astype(np.float32)

    def callback(self, indata, outdata, frames, t, status):
        p = self.p
        # ignore the device's first buffers entirely so junk never reaches the filter state
        x = np.nan_to_num(indata.astype(np.float64)) if self.blocks >= 8 else np.zeros((frames, 2))
        if p[VERSION] != self.version:
            self.version = p[VERSION]
            self.sos = design(p)
        if p[ENABLED] > 0.5:
            x, self.zi = sosfilt(self.sos, x, axis=0, zi=self.zi)
            width = 1 + 1.6 * float(p[SURROUND])
            mid, side = (x[:, 0] + x[:, 1]) * 0.5, (x[:, 0] - x[:, 1]) * 0.5 * width
            x = np.stack([mid + side, mid - side], 1)
            boost = float(p[BOOST])
            # loudness maximiser: a compressor evens out the dynamics first (quiet parts come up,
            # loud parts come down), then the boost lifts it all and the limiter catches peaks
            if boost > 0.01:
                lvl = 10 * math.log10(float(np.mean(x * x)) + 1e-12)
                ratio = 1 + 3.0 * boost
                over = lvl - (-30.0)
                want = -over * (1 - 1 / ratio) if over > 0 else 0.0
                coef = 0.5 if want < self.comp_db else 0.05  # fast attack, slow release (per block)
                start = self.comp_db
                self.comp_db += (want - self.comp_db) * coef
                makeup = MAX_BOOST_DB * boost + 0.6 * (-self.comp_db) * boost
                x *= np.linspace(10 ** ((start + makeup) / 20), 10 ** ((self.comp_db + makeup) / 20), len(x))[:, None]
        x = x.astype(np.float32)

        # look-ahead limiter: output the previous block, with gain chosen knowing the next one
        out = self.delay
        peak = max(float(np.abs(out).max(initial=0)), float(np.abs(x).max(initial=0)), 1e-9)
        target = min(1.0, CEILING / peak)
        if target < self.gain:
            ramp = np.linspace(self.gain, target, len(out), dtype=np.float32)
            own = min(1.0, CEILING / max(float(np.abs(out).max(initial=0)), 1e-9))
            ramp = np.minimum(ramp, own)
        else:  # release ~150 ms
            end = self.gain + (target - self.gain) * (1 - math.exp(-len(out) / (0.15 * RATE)))
            ramp = np.linspace(self.gain, end, len(out), dtype=np.float32)
        self.gain = float(ramp[-1])
        y = out * ramp[:, None]
        np.clip(y, -CEILING, CEILING, out=y)
        if self.blocks < 12:  # the device's first buffers can hold junk: stay silent, then fade in
            y *= max(0.0, (self.blocks - 8) / 4)
            self.gain = min(self.gain, 1.0)
        # Windows volume / mute, ramped over the block so slider moves never click.
        want = min(1.0, max(0.0, float(p[VOLUME])))
        outdata[:] = y * np.linspace(self.volume, want, len(y), dtype=np.float32)[:, None]
        self.volume = want
        self.delay = x

        # spectrum for the glass visualizer (~30 Hz), before the volume so it
        # keeps moving with the music at any listening level
        mono = y.mean(1)
        self.ring = np.roll(self.ring, -len(mono))
        self.ring[-len(mono):] = mono
        self.blocks += 1
        if self.blocks % 3 == 0:
            mag = np.abs(np.fft.rfft(self.ring * self.window)) / 512
            bands = np.array([mag[ix].max() for ix in self.band_idx])
            db = 20 * np.log10(bands + 1e-7)
            p[SPEC0:SPEC0 + N_BANDS] = np.clip((db + 62) / 56, 0, 1)
            p[LEVEL] = float(np.abs(y).max(initial=0))


def find_device(name, kind):
    requested = str(name or "").casefold()
    cable_output = "cable output" in requested or requested.startswith("cable out")
    for i, d in enumerate(sd.query_devices()):
        candidate = str(d["name"] or "")
        low = candidate.casefold()
        same_name = candidate == name
        same_cable = (cable_output and "vb-audio" in low and
                      (low.startswith("cable out") or "cable output" in low))
        if sd.query_hostapis(d["hostapi"])["name"].startswith("Windows WASAPI") and \
                (same_name or same_cable) and d[f"max_{kind}_channels"] >= 2:
            return i
    raise RuntimeError(f"device not found: {name}")


def restore_default(endpoint_id):
    """Give the user their audio back: real device default again, at the volume they had set."""
    try:
        from pycaw.pycaw import AudioUtilities
        from pycaw.constants import ERole
        vol = None
        for d in AudioUtilities.GetAllDevices(data_flow=0, device_state=1):
            name = str(d.FriendlyName or "").casefold()
            if "vb-audio virtual cable" in name or (
                    "vb-audio" in name and (name.startswith("cable in") or "cable input" in name)):
                vol = d.EndpointVolume.GetMasterVolumeLevelScalar()
        for d in AudioUtilities.GetAllDevices(data_flow=0, device_state=1):
            if d.id == endpoint_id and vol is not None:
                d.EndpointVolume.SetMasterVolumeLevelScalar(vol, None)
        AudioUtilities.SetDefaultDevice(endpoint_id, [ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])
    except Exception:
        pass


def main():
    out_name, out_id = sys.argv[1], sys.argv[2]
    shm = shared_memory.SharedMemory(name=sys.argv[3] if len(sys.argv) > 3 else SHM_NAME)
    eng = Engine(shm)
    p = eng.p
    try:
        stream = sd.Stream(device=(find_device("CABLE Output (VB-Audio Virtual Cable)", "input"),
                                   find_device(out_name, "output")),
                           samplerate=RATE, blocksize=BLOCK, channels=2, dtype="float32",
                           latency="low", callback=eng.callback)
        stream.start()
        p[STATUS] = 1
    except Exception:
        p[STATUS] = -1
        restore_default(out_id)
        return
    try:
        while True:
            time.sleep(0.25)
            if p[QUIT] > 0.5:
                break
            if time.time() - p[HEARTBEAT] > 5:  # the app died: give the user their audio back
                restore_default(out_id)
                break
            if not stream.active:
                p[STATUS] = -1
                restore_default(out_id)
                break
    finally:
        stream.close()
        p[STATUS] = 0
        p[SPEC0:SPEC0 + N_BANDS] = 0
        eng.p = p = None  # drop views into the shared buffer before closing it
        shm.close()


if __name__ == "__main__":
    main()
