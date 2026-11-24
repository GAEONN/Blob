"""Blob sound engine — runs as its own process so audio never stutters because of the UI.

Windows plays everything into the VB-Audio virtual cable; we read it back from "CABLE Output",
process it and play it on the real output device (held at 100 %; your volume slider lives on
the cable). VB-CABLE reports hardware volume, so Windows never attenuates what goes through it
and the cable itself ignores its own slider and mute. The app mirrors them into VOLUME and we
apply them here. Parameters and the spectrum are exchanged through shared memory.

Chain (v6.1, tuned to be clearly audible on laptop speakers, like FxSound):
  sub-sonic cut → preset EQ → bass (shelf + punch + psychoacoustic harmonics) → clarity
  (air shelf + presence) → surround (widen only above 180 Hz, bass stays centred) →
  leveler (lifts quiet content) → drive → 1.3 ms look-ahead limiter → soft clipper → volume.

Small speakers cannot play below ~150 Hz, so a plain bass shelf mostly wastes headroom and
makes everything quieter after the limiter. The harmonics generator re-creates the missing
fundamental an octave up where the speaker can play it, which is how FxSound and Waves MaxxBass
make bass "appear" on a laptop.

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

# The UI process imports the slot constants below. scipy.signal and PortAudio take ~1.1 s to
# load, so only the engine process loads them (see _load_dsp), keeping the app's launch fast.
sd = None
sosfilt = None

SHM_NAME = "BlobSound"
# shared-memory slots (float64)
HEARTBEAT, ENABLED, BOOST, BASS, CLARITY, SURROUND, VERSION, STATUS, QUIT, LEVEL = range(10)
GAIN_REDUCTION = 10   # limiter gain reduction in dB (diagnostics)
VOLUME = 12   # linear gain from the cable's Windows volume and mute; the app writes it
EQ0, SPEC0 = 16, 32
EQ_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
N_BANDS = 28
RATE, BLOCK = 48000, 480

# Loudness: what the Boost slider does at 100 %
MAX_DRIVE_DB = 18.0      # pushed into the limiter (louder, denser)
MAX_LIFT_DB = 12.0       # leveler: how far quiet content is raised
MAX_CUT_DB = 4.0         # leveler: how far loud content is lowered before the drive
LEVEL_TARGET_DB = -15.0  # leveler target (RMS, dBFS)
LEVEL_GATE_DB = -52.0    # below this it is silence/noise: never pumped up
MAX_BOOST_DB = MAX_DRIVE_DB + MAX_LIFT_DB   # shown in the UI as "+N dB"

# Limiter
CHUNK = 16               # gain is computed per 16 samples (0.33 ms) and interpolated
LOOKAHEAD = 4            # chunks (1.33 ms): the gain is already down when a peak arrives
LIMIT = 10 ** (-0.6 / 20)
RELEASE_S = 0.050
CLIP_KNEE, CLIP_TOP = 0.76, 0.985


def _load_dsp():
    global sd, sosfilt
    if sosfilt is None:
        import sounddevice
        from scipy.signal import sosfilt as _sosfilt
        sd, sosfilt = sounddevice, _sosfilt


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


def highpass(f, q=0.7071):
    w = 2 * math.pi * f / RATE
    al, c = math.sin(w) / (2 * q), math.cos(w)
    return _norm([(1 + c) / 2, -(1 + c), (1 + c) / 2], [1 + al, -2 * c, 1 - al])


def lowpass(f, q=0.7071):
    w = 2 * math.pi * f / RATE
    al, c = math.sin(w) / (2 * q), math.cos(w)
    return _norm([(1 - c) / 2, 1 - c, (1 - c) / 2], [1 + al, -2 * c, 1 - al])


def design(p):
    """Fixed number of sections so filter state carries over smoothly when settings change."""
    bass, clarity = float(p[BASS]), float(p[CLARITY])
    sections = [highpass(28)]                                                   # sub-sonic cut
    sections += [peaking(f, float(p[EQ0 + i]), 1.1) for i, f in enumerate(EQ_FREQS)]
    sections.append(shelf(110, 7.0 * bass, high=False))                         # weight (headphones)
    sections.append(peaking(150, 5.0 * bass, 0.9))                              # punch speakers can play
    sections.append(peaking(350, -1.5 * bass, 0.8))                             # keep it from going muddy
    sections.append(shelf(5000, 9.0 * clarity, high=True))                      # air
    sections.append(peaking(3000, 4.5 * clarity, 0.9))                          # presence / vocals
    return np.array(sections, np.float64)


# fixed filters for the harmonic bass generator and the side-channel widener
HARM_IN = np.array([highpass(40), lowpass(130), lowpass(130)], np.float64)    # fundamentals to recreate
HARM_OUT = np.array([highpass(110), highpass(110), lowpass(420)], np.float64)  # keep 2nd/3rd harmonics
SIDE_HP = np.array([highpass(180)], np.float64)


# ─────────────────────────── engine ───────────────────────────
class Engine:
    def __init__(self, shm):
        _load_dsp()
        self.p = np.ndarray((64,), np.float64, buffer=shm.buf)
        self.version = -1
        self.sos = design(self.p)
        self.zi = np.zeros((len(self.sos), 2, 2))
        self.zi_hin = np.zeros((len(HARM_IN), 2))
        self.zi_hout = np.zeros((len(HARM_OUT), 2))
        self.zi_side = np.zeros((len(SIDE_HP), 2))
        self.harm_gain = 0.0
        self.width = 1.0
        self.level_db = -30.0       # leveler's smoothed loudness estimate
        self.lift_db = 0.0          # leveler's current gain
        self.drive_db = 0.0
        # limiter state
        self.lim_t = np.ones(LOOKAHEAD)
        self.lim_h = np.ones(LOOKAHEAD)
        self.lim_last = 1.0
        self.lim_edge = 1.0
        self.lim_sig = np.zeros((LOOKAHEAD * CHUNK, 2))
        self.lim_rel = 1 - math.exp(-CHUNK / (RELEASE_S * RATE))
        self.volume = 1.0
        self.ring = np.zeros(2048, np.float32)
        self.blocks = 0
        edges = np.geomspace(40, 16000, N_BANDS + 1)
        freqs = np.fft.rfftfreq(2048, 1 / RATE)
        self.band_idx = [np.where((freqs >= edges[i]) & (freqs < edges[i + 1]))[0] for i in range(N_BANDS)]
        self.band_idx = [ix if len(ix) else np.array([np.argmin(abs(freqs - edges[i]))])
                         for i, ix in enumerate(self.band_idx)]
        self.window = np.hanning(2048).astype(np.float32)

    # ── stages ──
    def enhance(self, x, p):
        x, self.zi = sosfilt(self.sos, x, axis=0, zi=self.zi)
        mid = (x[:, 0] + x[:, 1]) * 0.5
        side = (x[:, 0] - x[:, 1]) * 0.5

        # psychoacoustic bass: |x| of the 40–130 Hz band is an octave-up copy of it whose level
        # follows the original exactly (|x| is linear in amplitude), so it never "breathes"
        low, self.zi_hin = sosfilt(HARM_IN, mid, zi=self.zi_hin)
        harm, self.zi_hout = sosfilt(HARM_OUT, np.abs(low), zi=self.zi_hout)
        want = 2.4 * float(p[BASS])
        mid = mid + harm * np.linspace(self.harm_gain, want, len(mid))
        self.harm_gain = want

        # surround: widen the stereo image above 180 Hz only, so bass stays solid and centred
        hi_side, self.zi_side = sosfilt(SIDE_HP, side, zi=self.zi_side)
        want = 1.0 + 2.2 * float(p[SURROUND])
        side = side + hi_side * (np.linspace(self.width, want, len(side)) - 1.0)
        self.width = want
        return np.stack([mid + side, mid - side], 1)

    def loudness(self, x, boost):
        """Leveler + drive. Returns x with a gain ramp applied (continuous across blocks)."""
        rms_db = 10 * math.log10(float(np.mean(x * x)) + 1e-12)
        if rms_db > LEVEL_GATE_DB:
            coef = 0.25 if rms_db > self.level_db else 0.03   # ~40 ms up, ~330 ms down
            self.level_db += (rms_db - self.level_db) * coef
            lift = min(MAX_LIFT_DB, max(-MAX_CUT_DB, LEVEL_TARGET_DB - self.level_db)) * boost
        else:
            lift = self.lift_db * 0.995  # silence: hold, drift slowly, never pump noise up
        drive = MAX_DRIVE_DB * boost
        start = self.lift_db + self.drive_db
        # limit how fast the leveler can move (≤ 0.25 dB per 10 ms) so it never pumps
        self.lift_db += max(-0.25, min(0.25, lift - self.lift_db))
        self.drive_db += max(-0.5, min(0.5, drive - self.drive_db))
        end = self.lift_db + self.drive_db
        if abs(start) < 1e-4 and abs(end) < 1e-4:
            return x
        return x * np.linspace(10 ** (start / 20), 10 ** (end / 20), len(x))[:, None]

    def limit(self, x):
        """Look-ahead brickwall limiter: delays the signal LOOKAHEAD chunks and lowers the gain
        just before each peak, so nothing clips and transients are not smeared."""
        n = len(x) // CHUNK
        pk = np.abs(x[:n * CHUNK]).max(1).reshape(n, CHUNK).max(1)
        t = np.minimum(1.0, LIMIT / np.maximum(pk, 1e-9))
        T = np.concatenate([self.lim_t, t])                       # T[j] is output chunk j
        need = np.lib.stride_tricks.sliding_window_view(T, LOOKAHEAD + 1).min(1)
        h = np.empty(n)
        last, rel = self.lim_last, self.lim_rel
        for j in range(n):                                        # 30 iterations per block
            last = min(need[j], last + (1.0 - last) * rel)
            h[j] = last
        self.lim_last = last
        H = np.concatenate([self.lim_h, h])
        s = np.lib.stride_tricks.sliding_window_view(H, LOOKAHEAD + 1).mean(1)
        self.lim_t, self.lim_h = T[-LOOKAHEAD:], H[-LOOKAHEAD:]
        # gain at each chunk boundary never exceeds either neighbour → peaks stay under LIMIT
        edges = np.empty(n + 1)
        edges[0] = self.lim_edge
        edges[1:n] = np.minimum(s[:-1], s[1:])
        edges[n] = s[-1]
        self.lim_edge = edges[n]
        g = np.interp(np.arange(n * CHUNK), np.arange(n + 1) * CHUNK, edges)
        S = np.concatenate([self.lim_sig, x])
        out, self.lim_sig = S[:n * CHUNK], S[n * CHUNK:]
        return out * g[:, None], float(g.min())

    @staticmethod
    def soft_clip(y):
        a = np.abs(y)
        over = a > CLIP_KNEE
        if over.any():
            r = CLIP_TOP - CLIP_KNEE
            y[over] = np.sign(y[over]) * (CLIP_KNEE + r * np.tanh((a[over] - CLIP_KNEE) / r))
        return y

    def process(self, x):
        """x: (frames, 2) float64 → (frames, 2) float64. Pure DSP, also used by the offline test."""
        p = self.p
        if p[VERSION] != self.version:
            self.version = p[VERSION]
            self.sos = design(p)
        if p[ENABLED] > 0.5:
            x = self.enhance(x, p)
            x = self.loudness(x, float(p[BOOST]))
        y, gmin = self.limit(x)
        p[GAIN_REDUCTION] = -20 * math.log10(max(gmin, 1e-6))
        return self.soft_clip(y)

    def callback(self, indata, outdata, frames, t, status):
        p = self.p
        # ignore the device's first buffers entirely so junk never reaches the filter state
        x = np.nan_to_num(indata.astype(np.float64)) if self.blocks >= 8 else np.zeros((frames, 2))
        y = self.process(x).astype(np.float32)
        if self.blocks < 12:  # the device's first buffers can hold junk: stay silent, then fade in
            y *= max(0.0, (self.blocks - 8) / 4)
        # Windows volume / mute, ramped over the block so slider moves never click.
        want = min(1.0, max(0.0, float(p[VOLUME])))
        outdata[:] = y * np.linspace(self.volume, want, len(y), dtype=np.float32)[:, None]
        self.volume = want

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
