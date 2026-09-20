"""Blob — tray app. The taskbar shows the CPU temperature; click it for a liquid-glass
panel with two pages: Blob (CPU/GPU temperatures, fans, fan mode) and Sound (system-wide
boost / EQ like FxSound, with a glass spectrum). Drag the panel to pin it
anywhere (e.g. over a game or video); click the tray number again to hide it."""
import ctypes
import os
import sys
import threading
import time
import winreg
from ctypes import wintypes

import numpy as np
import pystray
from PIL import Image, ImageDraw, ImageFont

import engine
import glass
from applemusic import AppleMusic
from media import NowPlaying
from sound import PRESET_ORDER, Sound
from glass import user32

APP_NAME = "Blob"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
FONTS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts")

k32 = ctypes.windll.kernel32
_mutex = k32.CreateMutexW(None, False, "Local\\BlobTrayApp")
if k32.GetLastError() == 183:  # already running
    sys.exit(0)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except OSError:
    pass

# ─────────────────────────── Win32 plumbing ───────────────────────────
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.SetCursor.argtypes = [wintypes.HANDLE]
user32.MonitorFromPoint.restype = wintypes.HANDLE
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]

WM_DESTROY, WM_ACTIVATE, WM_SETCURSOR, WM_KEYDOWN, WM_TIMER = 0x02, 0x06, 0x20, 0x100, 0x113
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_CAPTURECHANGED = 0x200, 0x201, 0x202, 0x215
WM_APP_TOGGLE, WM_APP_EXIT, WM_APP_REFOCUS = 0x8001, 0x8002, 0x8003


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]


def send_keys(seq):
    """seq: [(vk, down), ...] injected as one batch."""
    arr = (INPUT * len(seq))()
    for i, (vk, down) in enumerate(seq):
        arr[i].type = 1
        arr[i].ki = KEYBDINPUT(vk, 0, 0 if down else 2, 0, 0)
    user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))


def snipping_active():
    names = {"screenclippinghost.exe"}
    import psutil
    return any((p.info["name"] or "").lower() in names for p in psutil.process_iter(["name"]))


def album_tint(art):
    """Two colours from the artwork (upper and lower half) for a soft wash over the glass,
    like Apple Music's now-playing backdrop."""
    if art is None:
        return ((0, 0, 0, 0), None)
    small = np.asarray(art.resize((16, 16)), np.float32) / 255

    def dominant(px):
        px = px.reshape(-1, 3)
        w = (px.max(1) - px.min(1)) + 0.05
        return (px * w[:, None]).sum(0) / w.sum()
    top, bottom = dominant(small[:8]), dominant(small[8:])
    return ((float(top[0]), float(top[1]), float(top[2]), 0.24), tuple(float(v) for v in bottom))


def S_of(panel):
    return panel.S


def rect_gap(x, y, rect):
    """Distance from a point to a rectangle (0 inside)."""
    dx = max(rect[0] - x, 0, x - rect[2])
    dy = max(rect[1] - y, 0, y - rect[3])
    return (dx * dx + dy * dy) ** 0.5


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("hwndTrack", wintypes.HWND),
                ("dwHoverTime", wintypes.DWORD)]


def cursor_pos():
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def work_area_at(x, y):
    mi = MONITORINFO(ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromPoint(wintypes.POINT(x, y), 2), ctypes.byref(mi))
    return mi.rcWork, mi.rcMonitor


# ─────────────────────────── settings ───────────────────────────
def reg_dword(path, name, default):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return default


LIGHT_TASKBAR = reg_dword(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "SystemUsesLightTheme", 0) == 1


def startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_startup(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{pythonw}" "{os.path.abspath(__file__)}" --startup')
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


# ─────────────────────────── tray icon ───────────────────────────
def tray_image(temp):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    text = "--" if temp is None else str(round(temp))
    if temp is not None and temp >= 90:
        color = (255, 99, 82, 255)
    elif temp is not None and temp >= 80:
        color = (252, 176, 64, 255)
    else:
        color = (20, 20, 20, 255) if LIGHT_TASKBAR else (255, 255, 255, 255)
    try:
        font = ImageFont.truetype(os.path.join(FONTS, "seguisb.ttf"), 52 if len(text) <= 2 else 38)
    except OSError:
        font = ImageFont.load_default()
    box = d.textbbox((0, 0), text, font=font)
    d.text(((size - box[2] + box[0]) / 2 - box[0], (size - box[3] + box[1]) / 2 - box[1]), text, font=font, fill=color)
    return img


# ─────────────────────────── panel content ───────────────────────────
WARN, HOT = (255, 190, 90, 255), (255, 120, 105, 255)
MODES = [("auto", "Auto"), ("silent", "Silent"), ("balanced", "Balanced"), ("turbo", "Turbo")]


def fmt_temp(t):
    return "–" if t is None else f"{round(t)}°"


def fmt_rpm(r):
    return "–" if r is None else "Off" if r == 0 else f"{r:,} rpm"


class Fonts:
    def __init__(self, S):
        path = os.path.join(FONTS, "SegUIVar.ttf")
        self.cache, self.path, self.S = {}, path, S
        self.icons = os.path.join(FONTS, "SegoeIcons.ttf")

    def get(self, px, style="Regular"):
        key = (px, style)
        if key not in self.cache:
            try:
                f = ImageFont.truetype(self.path, round(px * self.S))
                f.set_variation_by_name(style)
            except OSError:
                f = ImageFont.truetype(os.path.join(FONTS, "segoeui.ttf"), round(px * self.S))
            self.cache[key] = f
        return self.cache[key]

    def emoji(self, px):
        key = (px, "emoji")
        if key not in self.cache:
            self.cache[key] = ImageFont.truetype(os.path.join(FONTS, "seguiemj.ttf"), round(px * self.S))
        return self.cache[key]

    def icon(self, px):
        key = (px, "icon")
        if key not in self.cache:
            self.cache[key] = ImageFont.truetype(self.icons, round(px * self.S))
        return self.cache[key]


def short_device(name):
    """'Speakers (Realtek(R) Audio)' → 'Speakers · Realtek'."""
    if not name:
        return "No output"
    if "(" in name:
        head, rest = name.split("(", 1)
        brand = rest.rstrip(")").replace("(R)", "").replace("High Definition Audio", "").replace(" Audio", "").strip()
        return f"{head.strip()} · {brand}" if brand else head.strip()
    return name


PROFILE = {"_t": time.time()} if os.environ.get("BLOB_PROFILE") else None

# ─────────────────────────── physics ───────────────────────────
class Spring:
    """Damped spring. zeta < 1 overshoots a little — that bounce is what makes glass feel liquid."""

    def __init__(self, value=0.0, k=300.0, zeta=0.7):
        self.x = self.target = float(value)
        self.v = 0.0
        self.k, self.c = k, 2 * zeta * k ** 0.5

    def step(self, dt):
        n = max(1, int(dt / 0.004) + 1)
        h = dt / n
        for _ in range(n):
            a = self.k * (self.target - self.x) - self.c * self.v
            self.v += a * h
            self.x += self.v * h
        if abs(self.x - self.target) < 0.01 and abs(self.v) < 0.05:
            self.x, self.v = self.target, 0.0

    @property
    def moving(self):
        return self.x != self.target or self.v != 0.0


class Springs(dict):
    def get(self, key, value=0.0, k=300.0, zeta=0.7):
        if key not in self:
            self[key] = Spring(value, k, zeta)
        return self[key]

    def step(self, dt):
        for s in self.values():
            s.step(dt)

    @property
    def moving(self):
        return any(s.moving for s in self.values())


# ─────────────────────────── panel ───────────────────────────
PAGES = [("blob", "Blob"), ("sound", "Sound"), ("music", "Music"), ("settings", "Settings")]
POINTERS = [("arrow", "Arrow"), ("triangle", "Triangle"), ("droplet", "Droplet"), ("system", "System")]
SIZES = [("regular", "Regular"), ("compact", "Compact")]


class Panel:
    """Lays out one page: draws text into an ink layer and describes the glass controls.
    Glass controls are resolved into lenses every frame by App (so they can animate)."""

    TOP = 56    # room taken by the tab bar (which sits at the bottom)
    CONTENT = 18  # content starts this far below the top edge
    WIDE, NARROW = 340, 248
    SS = 2          # content is drawn at 2x and filtered down on the GPU

    def __init__(self, S):
        self.S = S * self.SS      # every layout number below is in supersampled pixels
        self.f = Fonts(self.S)    # fonts scale with the supersampled layout too
        self.compact = False
        self.w = round(self.WIDE * self.S)
        self.page = "blob"
        self.music_view = "now"
        self.query = ""
        self.scroll = {}
        self.am = None
        self.details_open = False
        self.rects = {}
        self.sliders = {}

    def set_compact(self, compact):
        self.compact = compact
        self.w = round((self.NARROW if compact else self.WIDE) * self.S)

    def height(self, s, page=None):
        page, c = page or self.page, self.compact
        if page == "music":
            v = self.music_view
            if v == "art":
                return round((self.TOP + 44 + (self.w / self.S - 2 * self.pad_u)) * self.S)
            if v in ("search", "queue"):
                return round((self.TOP + (330 if c else 470)) * self.S)
            return round((self.TOP + (132 if c else 540)) * self.S)
        if page == "sound":
            return round((self.TOP + (150 if c else 462)) * self.S)
        if page == "settings":
            return round((self.TOP + (236 if c else 312)) * self.S)
        base = 152 if c else 292
        if self.details_open and s and not c:
            base = 286 + len(self.detail_rows(s)) * 24 + 16
        return round((base + self.TOP) * self.S)

    @property
    def pad_u(self):
        return 14 if self.compact else 22

    def max_height(self):
        return round((self.TOP + 300 + 16 * 24) * self.S)

    def detail_rows(self, s):
        cpu, gpu = s["cpu"], s["gpu"]
        awake = gpu["state"] != "sleeping"
        rows = [("CPU load", f"{round(cpu['load'])}%" if cpu["load"] is not None else "–", None),
                ("CPU clock", f"{cpu['clock'] / 1000:.2f} GHz" if cpu["clock"] else "–", None),
                ("GPU load", f"{round(gpu['load'])}%" if gpu["load"] is not None and awake else "–", None),
                ("GPU power", f"{gpu['power']:.0f} W" if gpu["power"] is not None else "–", None)]
        for x in s["sensors"]:
            name = x["name"].replace("AMD ", "").replace(" Graphics", "")
            if x["unit"] == "%" and "Radeon" in name:
                name += " load"
            rows.append((name, f"{x['value']}{x['unit']}", x["value"] if x.get("temp") else None))
        p = s["power"]
        if p["battery"] is not None:
            rows.append(("Battery", f"{p['battery']}% · " + ("plugged in" if p["plugged"] else "on battery"), None))
        return rows

    def hit(self, x, y):
        for key, (x0, y0, x1, y1) in self.rects.items():
            if x0 <= x < x1 and y0 <= y < y1:
                return key
        return None

    def slider_value(self, key, x):
        x0, x1 = self.sliders[key]
        return (x - x0) / max(1, x1 - x0)

    # ── drawing helpers ──
    def _begin(self, H):
        self.pic = Image.new("RGBA", (self.w, H), (0, 0, 0, 0))
        self.ink = Image.new("L", (self.w, H), 0)
        self.accent = Image.new("RGBA", (self.w, H), (0, 0, 0, 0))
        self.di, self.da = ImageDraw.Draw(self.ink), ImageDraw.Draw(self.accent)
        self.controls, self.rects, self.sliders = [], {}, {}

    def label(self, x, y, t, size, style="Regular", a=175, anchor="la", temp=None, icon=False):
        font = self.f.icon(size) if icon else self.f.get(size, style)
        if not icon and any(ord(c) > 0x2500 for c in t):  # emoji & symbols: Segoe UI Emoji has them
            font = self.f.emoji(size)
        if temp is not None and temp >= 80:
            self.da.text((x, y), t, font=font, fill=HOT if temp >= 90 else WARN, anchor=anchor)
        else:
            self.di.text((x, y), t, font=font, fill=a, anchor=anchor)

    def ink_shape(self, box, r, a):
        """Filled capsule/squircle in ink (recoloured per pixel like text)."""
        x0, y0, x1, y1 = [int(round(v)) for v in box]
        if x1 - x0 < 1 or y1 - y0 < 1:
            return
        if x1 - x0 < 4 or y1 - y0 < 4:      # hairlines: rounding would be invisible anyway
            self.di.rectangle((x0, y0, x1 - 1, y1 - 1), fill=a)
            return
        m = Image.fromarray((glass.shape_mask(x1 - x0, y1 - y0, r) * 255).astype(np.uint8))
        self.ink.paste(a, (x0, y0, x1, y1), m)

    def static(self, rect, r, **kw):
        self.controls.append(("static", dict(rect=rect, r=r, **kw)))  # kw may include n (corner shape)

    def hover_lens(self, key, rect, r):
        self.rects[key] = rect
        self.controls.append(("hover", key, rect, r))

    def segmented(self, prefix, options, selected, box, font_size=13, running=None):
        """Frosted capsule track; the thumb is animated by App (slides, stretches, settles)."""
        S = self.S
        tx0, ty0, tx1, ty1 = box
        th = ty1 - ty0
        self.static(box, th / 2, strength=6 * S, bevel=9 * S, rim=0.6, frost=1.0, lift=0.05)
        opts = [(o[0], o[1], o[2] if len(o) > 2 else 1.0) for o in options]
        total = sum(o[2] for o in opts)
        x, segs, sel = tx0, [], 0
        for i, (key, name, wgt) in enumerate(opts):
            sw = (tx1 - tx0) * wgt / total
            segs.append((x, x + sw))
            self.rects[f"{prefix}:{key}"] = (x, ty0, x + sw, ty1)
            on = key == selected
            if not on:
                ins = 3 * S
                self.controls.append(("hover", f"{prefix}:{key}", (x + ins, ty0 + ins, x + sw - ins, ty1 - ins),
                                      th / 2 - ins))
            if on:
                sel = i
            cx = x + sw / 2
            icon = len(name) == 1 and ord(name) > 0xE000
            self.label(cx, ty0 + th / 2, name, 11 if icon else font_size, "Semibold Text" if on else "Regular",
                       255 if on else 175, "mm", icon=icon)
            if running == key:
                r, cy = 2 * S, ty1 - 3 * S - 6 * S
                self.di.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
            x += sw
        self.controls.append(("seg", prefix, box, segs, sel))

    def slider(self, key, value, x0, x1, cy):
        S = self.S
        th = 6 * S
        self.static((x0, cy - th / 2, x1, cy + th / 2), th / 2, strength=2 * S, bevel=3 * S, rim=0.4,
                    frost=1.0, lift=0.05)
        self.sliders[key] = (x0, x1)
        self.rects[f"slider:{key}"] = (x0 - 14 * S, cy - 18 * S, x1 + 14 * S, cy + 18 * S)
        self.controls.append(("slider", key, x0, x1, cy, max(0.0, min(1.0, value))))

    def toggle(self, key, on, x0, y0):
        S = self.S
        box = (x0, y0, x0 + 48 * S, y0 + 28 * S)
        self.rects[f"toggle:{key}"] = box
        self.controls.append(("toggle", key, box, on))

    # ── pages ──
    def draw(self, s, snd, glassiness, startup, pointer="arrow", media=None, seek=None, captureable=False):
        S, W = self.S, self.w
        H = self.height(s)
        self._begin(H)
        pad = self.pad_u * S
        # tab bar lives at the bottom: the panel is bottom-anchored, so it never moves or resizes
        self.segmented("page", PAGES, self.page, (pad, H - 50 * S, W - pad, H - 16 * S),
                       11 if self.compact else 13)
        top = self.CONTENT * S
        if self.page == "sound":
            (self._sound_compact if self.compact else self._sound)(snd, pad, top)
        elif self.page == "settings":
            self._settings(glassiness, startup, pointer, pad, top, captureable)
        elif self.page == "music":
            self._music(media, snd, seek, pad, top)
        else:
            (self._blob_compact if self.compact else self._blob)(s, pad, top)
        return self.ink, self.accent, self.controls

    def fit(self, text, size, style, max_w):
        """Ellipsize text to max_w pixels."""
        font = self.f.get(size, style)
        if font.getlength(text) <= max_w:
            return text
        while text and font.getlength(text + "…") > max_w:
            text = text[:-1]
        return text.rstrip() + "…"

    def transport(self, key, cx, cy, r, kind, always=False, active=False):
        """Round glass button with a solid mark: shuffle, rewind, play/pause, forward, repeat."""
        S = self.S
        rect = (cx - r, cy - r, cx + r, cy + r)
        if always:
            self.static(rect, r, strength=7 * S, bevel=r * 0.8, zoom=0.9, rim=0.9, frost=1.0, lift=0.14,
                        raised=1.0)
        self.rects[key] = rect
        self.controls.append(("hover", key, rect, r))
        d, u, ink = self.di, r * 0.52, 255 if active or kind in ("play", "pause") else 225
        if kind == "pause":
            bw, bh = u * 0.34, u * 0.95
            for sx in (-1, 1):
                x = cx + sx * u * 0.42
                d.rounded_rectangle((x - bw / 2, cy - bh, x + bw / 2, cy + bh), radius=bw / 2, fill=ink)
        elif kind == "play":
            d.polygon([(cx - u * 0.55, cy - u), (cx - u * 0.55, cy + u), (cx + u * 0.95, cy)], fill=ink)
        elif kind in ("previous", "next"):
            sgn = -1 if kind == "previous" else 1          # two solid triangles, like Apple Music
            for i in (0, 1):
                tip = cx + sgn * (u * 1.05 - i * u * 1.0)
                back = tip - sgn * u * 0.95
                d.polygon([(back, cy - u * 0.9), (back, cy + u * 0.9), (tip, cy)], fill=ink)
        elif kind == "shuffle":
            w_, h_ = u * 1.15, u * 0.72
            lw = max(2, round(u * 0.26))
            for sy in (-1, 1):
                d.line([(cx - w_, cy + sy * h_), (cx - w_ * 0.35, cy + sy * h_),
                        (cx + w_ * 0.35, cy - sy * h_), (cx + w_ * 0.72, cy - sy * h_)],
                       fill=ink, width=lw, joint="curve")
                ax = cx + w_ * 0.72
                ay = cy - sy * h_
                d.polygon([(ax, ay - u * 0.42), (ax, ay + u * 0.42), (ax + u * 0.55, ay)], fill=ink)
        elif kind in ("repeat", "repeat_one"):
            w_, h_ = u * 1.0, u * 0.72
            lw = max(2, round(u * 0.26))
            d.rounded_rectangle((cx - w_, cy - h_, cx + w_, cy + h_), radius=h_ * 0.9, outline=ink, width=lw)
            d.rectangle((cx + w_ * 0.1, cy - h_ - lw, cx + w_ * 0.75, cy - h_ + lw), fill=0)
            d.polygon([(cx + w_ * 0.55, cy - h_ - u * 0.45), (cx + w_ * 0.55, cy - h_ + u * 0.45),
                       (cx + w_ * 1.05, cy - h_)], fill=ink)
            if kind == "repeat_one":
                d.text((cx, cy), "1", font=self.f.get(9, "Semibold Text"), fill=ink, anchor="mm")

    def glass_button(self, key, cx, cy, r, glyph, size, always=False):
        """Round glass button: fuses with the pointer; `always` keeps a frosted body when idle."""
        S = self.S
        rect = (cx - r, cy - r, cx + r, cy + r)
        if always:
            self.static(rect, r, strength=7 * S, bevel=r * 0.8, zoom=0.9, rim=0.9, frost=1.0, lift=0.14,
                        raised=1.0)
        self.rects[key] = rect
        self.controls.append(("hover", key, rect, r))
        self.di.text((cx, cy), glyph, font=self.f.icon(size), fill=255, anchor="mm")

    ROW_SEARCH, ROW_LIST = 50, 38

    def _music(self, m, snd, seek, pad, top):
        view = self.music_view
        if view == "search":
            return self._music_search(pad, top)
        if view == "queue":
            return self._music_queue(pad, top)
        if view == "art":
            return self._music_art(m, pad, top)
        if self.compact:
            return self._music_mini(m, seek, pad, top)
        S, W = self.S, self.w
        px = lambda v: top + v * S
        L = self.label
        a = round(W - 2 * pad)
        ax, ay = round(pad), round(px(2))
        self._artwork(m, ax, ay, a, a * 0.115, 44)
        ty = ay + a + 18 * S
        tw = W - 2 * pad - 80 * S
        if m and m.active:
            L(pad, ty, self.fit(m.title or "Unknown title", 18, "Semibold Text", tw), 18, "Semibold Text", 255)
            sub = " \u2014 ".join(x for x in (m.artist, m.album) if x)
            L(pad, ty + 26 * S, self.fit(sub or m.source, 13, "Regular", tw), 13, "Regular", 190)
        else:
            L(pad, ty, "Not playing", 18, "Semibold Text", 255)
            L(pad, ty + 26 * S, "Search for something to start.", 13, "Regular", 170)
        self.glass_button("mview:queue", W - pad - 15 * S, ty + 18 * S, 15 * S, "\uE8FD", 11, always=True)
        self.glass_button("mview:search", W - pad - 52 * S, ty + 18 * S, 15 * S, "\uE721", 11, always=True)

        py_ = ty + 66 * S
        self._progress(m, seek, pad + 12 * S, W - pad - 12 * S, py_, times=True)
        cy = py_ + 62 * S
        playing = bool(m and m.playing)
        am = self.am
        rep = getattr(am, "repeat", "off")
        self.transport("am:shuffle", W / 2 - 128 * S, cy, 17 * S, "shuffle",
                       active=bool(getattr(am, "shuffle", False)))
        self.transport("media:previous", W / 2 - 74 * S, cy, 24 * S, "previous")
        self.transport("media:toggle", W / 2, cy, 32 * S, "pause" if playing else "play", always=True)
        self.transport("media:next", W / 2 + 74 * S, cy, 24 * S, "next")
        self.transport("am:repeat", W / 2 + 128 * S, cy, 17 * S,
                       "repeat_one" if rep == "one" else "repeat", active=rep != "off")
        self._volume_row(snd, pad, cy + 58 * S)

    def _music_mini(self, m, seek, pad, top):
        """Mini player: artwork, one line of text, transport — for a screen corner."""
        S, W = self.S, self.w
        px = lambda v: top + v * S
        L = self.label
        a = round(54 * S)
        self._artwork(m, round(pad), round(px(2)), a, a * 0.2, 20)
        tx = pad + a + 12 * S
        tw = W - pad - tx - 58 * S      # leave room for the search / queue buttons
        if m and m.active:
            L(tx, px(8), self.fit(m.title or "", 13, "Semibold Text", tw), 13, "Semibold Text", 255)
            L(tx, px(26), self.fit(m.artist or m.source, 11, "Regular", tw), 11, "Regular", 180)
        else:
            L(tx, px(8), "Not playing", 13, "Semibold Text", 255)
            L(tx, px(26), "Tap search to start", 11, "Regular", 170)
        self._progress(m, seek, pad, W - pad, px(66), times=False)
        cy = px(98)
        playing = bool(m and m.playing)
        am = self.am
        rep = getattr(am, "repeat", "off")
        self.transport("am:shuffle", pad + 12 * S, cy, 12 * S, "shuffle",
                       active=bool(getattr(am, "shuffle", False)))
        self.transport("media:previous", pad + 52 * S, cy, 15 * S, "previous")
        self.transport("media:toggle", W / 2, cy, 21 * S, "pause" if playing else "play", always=True)
        self.transport("media:next", W - pad - 52 * S, cy, 15 * S, "next")
        self.transport("am:repeat", W - pad - 12 * S, cy, 12 * S,
                       "repeat_one" if rep == "one" else "repeat", active=rep != "off")
        self.glass_button("mview:search", W - pad - 22 * S, px(20), 13 * S, "\uE721", 10, always=True)
        self.glass_button("mview:queue", W - pad - 52 * S, px(20), 13 * S, "\uE8FD", 10, always=True)

    def _music_art(self, m, pad, top):
        """Just the artwork, Apple Music style. Click it again to go back."""
        S, W = self.S, self.w
        a = round(W - 2 * pad)
        self._artwork(m, round(pad), round(top), a, a * 0.115, 60)
        self._progress(m, None, pad, W - pad, top + a + 22 * S, times=False)

    def _artwork(self, m, ax, ay, a, radius, glyph_size):
        """Album art as a squircle with a glass rim; clicking it opens the artwork view."""
        art = m.art if (m and m.active) else None
        if art is not None:
            iw, ih = art.size
            side = min(iw, ih)   # centre-crop wide video thumbnails instead of squashing them
            img = art.crop(((iw - side) // 2, (ih - side) // 2, (iw + side) // 2, (ih + side) // 2))
            img = img.resize((a, a), Image.LANCZOS).convert("RGBA")
            img.putalpha(Image.fromarray((glass.shape_mask(a, a, radius) * 255).astype(np.uint8)))
            self.pic.alpha_composite(img, (ax, ay))
        else:
            self.static((ax, ay, ax + a, ay + a), radius, strength=8 * self.S, bevel=16 * self.S, rim=0.8,
                        frost=1.0, lift=0.12, n=5.0)
            self.di.text((ax + a / 2, ay + a / 2), "\uEC4F", font=self.f.icon(glyph_size), fill=140, anchor="mm")
        if art is None:   # only the placeholder needs a glass edge; real covers stand on their own
            self.static((ax, ay, ax + a, ay + a), radius, strength=4 * self.S, bevel=8 * self.S, rim=1.0,
                        n=5.0)
        self.rects["mview:" + ("now" if self.music_view == "art" else "art")] = (ax, ay, ax + a, ay + a)

    def _progress(self, m, seek, x0, x1, y, times=True):
        S = self.S
        dur = m.duration if (m and m.active) else 0.0
        pos = (seek * dur if seek is not None else m.pos_now()) if dur else 0.0
        frac = pos / dur if dur > 0 else 0.0
        if m and m.active and dur > 0:
            self.slider("seek", frac, x0, x1, y)
        else:
            th = 6 * S
            self.static((x0, y - th / 2, x1, y + th / 2), th / 2, frost=1.0, lift=0.05)
            if frac > 0:
                self.ink_shape((x0, y - th / 2, x0 + (x1 - x0) * frac, y + th / 2), th / 2, 200)
        if times:
            fmt = lambda t: f"{int(t // 60)}:{int(t % 60):02d}"
            self.label(x0, y + 16 * S, fmt(pos) if dur else "", 11, "Regular", 150)
            self.label(x1, y + 16 * S, ("-" + fmt(max(0.0, dur - pos))) if dur else "", 11, "Regular", 150, "ra")

    def _back_row(self, pad, top, title=None):
        S = self.S
        r = 13 * S if self.compact else 16 * S
        self.glass_button("mview:now", pad + r, top + 20 * S, r, "\uE72B", 10 if self.compact else 11, always=True)
        if title:
            self.label(pad + 2 * r + 12 * S, top + 20 * S, title, 14 if self.compact else 16, "Semibold Text",
                       255, "lm")

    def _list_rows(self, key, n, row_h, top, bottom):
        """Visible slice of a scrollable list: yields (index, y)."""
        S = self.S
        rows = max(1, int((bottom - top) // (row_h * S)))
        first = max(0, min(self.scroll.get(key, 0), max(0, n - rows)))
        self.scroll[key] = first
        for i in range(first, min(n, first + rows)):
            yield i, top + (i - first) * row_h * S
        if n > rows:  # a thin scroll indicator
            W = self.w
            h = (bottom - top) * rows / n
            y0 = top + (bottom - top - h) * first / max(1, n - rows)
            self.ink_shape((W - 9 * S, y0, W - 6 * S, y0 + h), 1.5 * S, 90)

    def _music_search(self, pad, top):
        S, W = self.S, self.w
        am, L = self.am, self.label
        self._back_row(pad, top)
        left = pad + (34 if self.compact else 40) * S
        box = (left, top + 2 * S, W - pad, top + 38 * S)
        self.static(box, (box[3] - box[1]) / 2, strength=5 * S, bevel=9 * S, rim=0.8, frost=1.0, lift=0.07)
        self.rects["searchbox"] = box
        self.di.text((box[0] + 14 * S, (box[1] + box[3]) / 2), "\uE721", font=self.f.icon(10), fill=150, anchor="lm")
        tx = box[0] + 30 * S
        size = 13 if self.compact else 14
        if self.query:
            shown = self.fit(self.query, size, "Regular", box[2] - tx - 14 * S)
            L(tx, (box[1] + box[3]) / 2, shown, size, "Regular", 255, "lm")
            cx = tx + self.f.get(size).getlength(shown) + 2 * S
        else:
            L(tx, (box[1] + box[3]) / 2, "Search Apple Music", size, "Regular", 120, "lm")
            cx = tx
        if int(time.time() * 2) % 2 == 0:
            self.ink_shape((cx, box[1] + 10 * S, cx + 1.5 * S, box[3] - 10 * S), 0.75 * S, 230)
        row_h = 44 if self.compact else self.ROW_SEARCH
        list_top, list_bottom = top + 50 * S, self.height(None) - (76 if self.compact else 90) * S
        results = am.results if am else []
        for i, y in self._list_rows("search", len(results), row_h, list_top, list_bottom):
            r = results[i]
            row = (pad - 8 * S, y, W - pad + 8 * S, y + (row_h - 4) * S)
            self.rects[f"result:{i}"] = row
            self.controls.append(("hover", f"result:{i}", row, 13 * S))
            th = round((36 if self.compact else 40) * S)
            if r.get("art") is not None:
                img = r["art"].resize((th, th), Image.LANCZOS).convert("RGBA")
                img.putalpha(Image.fromarray((glass.shape_mask(th, th, th * 0.22) * 255).astype(np.uint8)))
                self.pic.alpha_composite(img, (round(pad), round(y + 3 * S)))
            elif r.get("kind") == "playlist":
                self.di.text((pad + th / 2, y + 3 * S + th / 2), "\uE8FD", font=self.f.icon(12), fill=170,
                             anchor="mm")
            tx2 = pad + th + 12 * S
            tw = W - pad - tx2 - 8 * S
            L(tx2, y + (5 if self.compact else 7) * S, self.fit(r["title"], 13, "Semibold Text", tw), 13,
              "Semibold Text", 255)
            sub = r["artist"] + ((" \u2014 " + r["album"]) if r.get("album") else "")
            L(tx2, y + (23 if self.compact else 25) * S, self.fit(sub, 12, "Regular", tw), 12, "Regular", 165)
        msg = am.status if am else ""
        if not msg and not results:
            msg = "Type a song, artist or playlist, then Enter."
        L(pad, self.height(None) - (64 if self.compact else 76) * S, msg, 12, "Regular", 165)

    def _music_queue(self, pad, top):
        S, W = self.S, self.w
        am, L = self.am, self.label
        self._back_row(pad, top, "Playing Next")
        items = am.queue if am else []
        row_h = 34 if self.compact else 42
        list_top, list_bottom = top + 48 * S, self.height(None) - (76 if self.compact else 90) * S
        if not items:
            L(pad, list_top + 10 * S, "Nothing queued up.", 12, "Regular", 165)
        for i, y in self._list_rows("queue", len(items), row_h, list_top, list_bottom):
            q = items[i]
            row = (pad - 8 * S, y, W - pad + 8 * S, y + (row_h - 4) * S)
            self.rects[f"queue:{i}"] = row
            self.controls.append(("hover", f"queue:{i}", row, 12 * S))
            th = round((26 if self.compact else 32) * S)
            if q.get("art") is not None:
                img = q["art"].resize((th, th), Image.LANCZOS).convert("RGBA")
                img.putalpha(Image.fromarray((glass.shape_mask(th, th, th * 0.22) * 255).astype(np.uint8)))
                self.pic.alpha_composite(img, (round(pad), round(y + 3 * S)))
            else:
                self.static((pad, y + 3 * S, pad + th, y + 3 * S + th), th * 0.22, frost=1.0, lift=0.08,
                            n=5.0)
            tx = pad + th + 10 * S
            tw = W - pad - tx - 8 * S
            L(tx, y + (5 if self.compact else 3) * S, self.fit(q["title"], 13, "Semibold Text", tw), 13,
              "Semibold Text", 235)
            if not self.compact:
                L(tx, y + 20 * S, self.fit(q["artist"], 11, "Regular", tw), 11, "Regular", 160)
        L(pad, self.height(None) - (64 if self.compact else 76) * S, am.status if am else "", 12, "Regular", 165)

    def _volume_row(self, snd, pad, y):
        S, W = self.S, self.w
        self.di.text((pad + 6 * S, y), "\uE993", font=self.f.icon(12), fill=175, anchor="mm")
        self.di.text((W - pad - 6 * S, y), "\uE995", font=self.f.icon(12), fill=175, anchor="mm")
        self.slider("volume", snd.volume if snd.volume is not None else 0.5, pad + 30 * S, W - pad - 30 * S, y)

    def _blob_compact(self, s, pad, top):
        """Temperatures, fans and the fan mode, sized for a screen corner."""
        S, W = self.S, self.w
        px = lambda v: top + v * S
        L = self.label
        cpu, gpu, fans, ctl = s["cpu"], s["gpu"], s["fans"], s["control"]
        col2 = W / 2 + 4 * S
        L(pad, px(2), "CPU", 11)
        L(pad, px(14), fmt_temp(cpu["temp"]), 30, "Semibold Display", 255 if cpu["temp"] else 105,
          temp=cpu["temp"])
        L(pad, px(54), "Fan " + fmt_rpm(fans[0]["rpm"]), 11)
        asleep = gpu["state"] == "sleeping"
        L(col2, px(2), "GPU · off" if asleep else "GPU", 11)
        L(col2, px(14), "–" if asleep else fmt_temp(gpu["temp"]), 30, "Semibold Display",
          105 if asleep else 255, temp=None if asleep else gpu["temp"])
        if len(fans) > 1:
            L(col2, px(54), "Fan " + fmt_rpm(fans[1]["rpm"]), 11)
        self.di.rectangle((pad, px(80), W - pad, px(80) + max(1, round(S)) - 1), fill=45)
        L(pad, px(90), "Fan mode", 11)
        self.segmented("mode", MODES, ctl["mode"], (pad, px(106), W - pad, px(136)), 10,
                       running=ctl["active"] if ctl["mode"] == "auto" else None)

    def _sound_compact(self, snd, pad, top):
        """On/off, boost and the spectrum — the rest lives in the regular size."""
        S, W = self.S, self.w
        px = lambda v: top + v * S
        L = self.label
        L(pad, px(12), "Boost", 13, "Semibold Text", 255, "lm")
        L(W - pad - 58 * S, px(12), f"+{snd.boost_db:.0f} dB", 12, "Regular", 175, "rm")
        self.toggle("sound", snd.enabled, W - pad - 48 * S, px(-2))
        self.slider("boost", snd.boost, pad + 12 * S, W - pad - 12 * S, px(48))
        self.controls.append(("viz", (pad, px(70), W - pad, px(120))))

    def _blob(self, s, pad, top):
        S, W = self.S, self.w
        px = lambda v: top + v * S
        cpu, gpu, fans, ctl = s["cpu"], s["gpu"], s["fans"], s["control"]
        col2 = W / 2 + 6 * S
        L = self.label
        L(pad, px(8), "CPU", 13)
        L(pad, px(24), fmt_temp(cpu["temp"]), 44, "Semibold Display", 255 if cpu["temp"] else 105, temp=cpu["temp"])
        L(pad, px(82), "Fan " + fmt_rpm(fans[0]["rpm"]), 13)
        if gpu["state"] == "sleeping":
            L(col2, px(8), "GPU · asleep", 13)
            L(col2, px(24), "–", 44, "Semibold Display", 105)
        else:
            active = gpu["state"] == "active"
            L(col2, px(8), "GPU" if active else "GPU · idle", 13)
            L(col2, px(24), fmt_temp(gpu["temp"]), 44, "Semibold Display", 255 if active else 105,
              temp=gpu["temp"] if active else None)
        if len(fans) > 1:
            L(col2, px(82), "Fan " + fmt_rpm(fans[1]["rpm"]), 13)
        self.di.rectangle((pad, px(112), W - pad, px(112) + max(1, round(S)) - 1), fill=45)
        L(pad, px(126), "Fan mode", 13)
        self.segmented("mode", MODES, ctl["mode"], (pad, px(150), W - pad, px(186)),
                       running=ctl["active"] if ctl["mode"] == "auto" else None)
        active = (ctl["active"] or "").title()
        if ctl["mode"] != "auto":
            msg = f"Fixed on {ctl['mode'].title()}. Choose Auto to follow the load."
        elif active:
            msg = f"Auto is using {active}" + (f" · {ctl['reason']}" if ctl["reason"] else "")
        else:
            msg = "Auto is choosing a mode…"
        L(pad, px(198), msg, 12)
        self.di.rectangle((pad, px(228), W - pad, px(228) + max(1, round(S)) - 1), fill=45)
        row = (pad - 8 * S, px(236), W - pad + 8 * S, px(264))
        self.hover_lens("details", row, (row[3] - row[1]) / 2)
        L(pad, px(250), "More details", 14, "Regular", 255, "lm")
        self.di.text((W - pad, px(250)), "" if self.details_open else "", font=self.f.icon(10),
                     fill=175, anchor="rm")
        if self.details_open:
            y = px(280)
            for k, v, t in self.detail_rows(s):
                L(pad, y, k, 13)
                L(W - pad, y, v, 13, "Regular", 255, "ra", temp=t)
                y += 24 * S

    def _sound(self, snd, pad, top):
        S, W = self.S, self.w
        px = lambda v: top + v * S
        L = self.label
        L(pad, px(6), "Output", 12)
        self.hover_lens("device", (pad - 6 * S, px(20), W - pad - 64 * S, px(44)), 12 * S)
        L(pad, px(32), short_device(snd.output) + "  ›", 14, "Semibold Text", 255, "lm")
        self.toggle("sound", snd.enabled, W - pad - 48 * S, px(10))
        self.controls.append(("viz", (pad, px(58), W - pad, px(138))))

        L(pad, px(152), "Boost", 14, "Semibold Text", 255)
        L(W - pad, px(152), f"+{snd.boost_db:.1f} dB", 13, "Regular", 175, "ra")
        self.slider("boost", snd.boost, pad + 12 * S, W - pad - 12 * S, px(186))
        self.segmented("preset", PRESET_ORDER, snd.preset, (pad, px(212), W - pad, px(246)), 12)
        y = px(266)
        for key, name in (("bass", "Bass"), ("clarity", "Clarity"), ("surround", "Surround")):
            L(pad, y, name, 13)
            L(W - pad, y, f"{round(getattr(snd, key) * 100)}%", 12, "Regular", 140, "ra")
            self.slider(key, getattr(snd, key), pad + 12 * S, W - pad - 12 * S, y + 30 * S)
            y += 50 * S
        if snd.fx_conflict:
            bx = (W - pad - 104 * S, px(416), W - pad, px(444))
            self.rects["fxquit"] = bx
            self.static(bx, (bx[3] - bx[1]) / 2, strength=5 * S, bevel=8 * S, zoom=0.95, rim=0.9, frost=1.0,
                        lift=0.16, raised=1.0)
            L((bx[0] + bx[2]) / 2, px(430), "Quit FxSound", 12, "Semibold Text", 255, "mm")
            L(pad, px(430), "FxSound keeps taking the audio.", 12, anchor="lm")
            return
        if snd.error:
            msg = snd.error
        elif snd.enabled:
            msg = f"Enhancing everything you hear · +{snd.boost_db:.0f} dB boost"
        else:
            msg = "Off · turn on to boost and shape your audio"
        L(pad, px(430), msg, 12, anchor="lm")

    def _settings(self, glassiness, startup, pointer, pad, top, captureable=False):
        S, W = self.S, self.w
        c = self.compact
        px = lambda v: top + (v + 4) * S
        L = self.label
        L(pad, px(2), "Glass", 13 if c else 14, "Semibold Text", 255)
        L(W - pad, px(2), "Clear" if glassiness < 0.08 else "Frosted" if glassiness > 0.85
          else f"{round(glassiness * 100)}% frosted", 12, "Regular", 175, "ra")
        self.slider("glass", glassiness, pad + 12 * S, W - pad - 12 * S, px(32))
        L(pad, px(50), "Clear", 11, "Regular", 120)
        L(W - pad, px(50), "Frosted", 11, "Regular", 120, "ra")

        L(pad, px(84), "Size", 13 if c else 14, "Semibold Text", 255, "lm")
        self.segmented("size", SIZES, "compact" if c else "regular",
                       (W / 2 - 4 * S if c else W / 2 + 6 * S, px(70), W - pad, px(98)), 11)
        y = 112
        if not c:
            L(pad, px(y), "Pointer", 14, "Semibold Text", 255)
            self.segmented("pointer", POINTERS, pointer, (pad, px(y + 22), W - pad, px(y + 54)), 12)
            y += 80

        L(pad, px(y + 14), "Show in screen recordings", 12 if c else 14, "Regular", 255, "lm")
        self.toggle("capture", captureable, W - pad - 48 * S, px(y))
        L(pad, px(y + 30), "Glass stops updating while this is on.", 11, "Regular", 140)

        L(pad, px(y + 62), "Start with Windows", 12 if c else 14, "Regular", 255, "lm")
        self.toggle("startup", startup, W - pad - 48 * S, px(y + 48))


class App:
    def __init__(self):
        self.mon = engine.Monitor()
        threading.Thread(target=self.mon.run, daemon=True).start()
        self.sound = Sound()
        self.media = NowPlaying()
        self.am = AppleMusic(refocus=lambda: user32.PostMessageW(self.hwnd, WM_APP_REFOCUS, 0, 0))
        self.hover_since = (None, 0.0)
        self.last_queue = 0.0
        self.am_seen = -1
        self.hold_until = 0.0
        self.seek_value = None
        self.media_seen = None
        dpi = user32.GetDpiForSystem() if hasattr(user32, "GetDpiForSystem") else 96
        self.S = S = dpi / 96
        self.panel = Panel(S)
        self.panel.page = os.environ.get("BLOB_PAGE", "blob")  # debug: open on a given page
        self.panel.am = self.am
        self.panel.music_view = os.environ.get("BLOB_MVIEW", "now")      # debug hooks
        if os.environ.get("BLOB_QUERY"):
            self.panel.query = os.environ["BLOB_QUERY"]
            self.am.search(self.panel.query)
        # the window buffer is always sized for the wide panel; compact just draws narrower inside it
        self.ss = Panel.SS
        self.glass = glass.GlassRenderer(S, round(Panel.WIDE * S), round(self.panel.max_height() / self.ss))
        self.glass.set_supersample(self.ss)
        cfg = engine.load_config()
        self.glassiness = float(cfg.get("glass", 0.35))
        self.captureable = bool(cfg.get("captureable", False))
        self.panel.set_compact(os.environ.get("BLOB_COMPACT", "1" if cfg.get("compact") else "0") == "1")
        self.startup = startup_enabled()
        self.springs = Springs()
        self.controls, self.old_controls = [], []
        self.visible = self.pinned = False
        if PROFILE is not None:
            self.pinned = True  # profiling: keep the panel up even when focus moves elsewhere
        self.pos = None  # window top-left (screen px)
        self.drag = None
        self.slider_drag = None
        self.pressed = None
        self.hover = None
        self.mouse_in = False
        self.mouse_xy = None
        self.attached = self.detaching = None
        self.pointer_style = cfg.get("pointer", "arrow")
        self.last_hide = 0.0
        self.snap = None
        self.light = np.array([-0.55, -0.83])
        self.vel = np.zeros(2)
        self.last_frame = time.perf_counter()
        self.last_text = 0.0
        self.interval = 0
        self.frame_dirty = True

        ctypes.windll.winmm.timeBeginPeriod(1)  # 1 ms timers: even frame pacing
        self.capture_until = 0.0
        self.pending_keys = None
        self._hookproc = HOOKPROC(self._keyboard_hook)
        self._hook = user32.SetWindowsHookExW(13, self._hookproc, k32.GetModuleHandleW(None), 0)

        self._wndproc = WNDPROC(self.wndproc)
        hinst = k32.GetModuleHandleW(None)
        wc = WNDCLASSEXW(ctypes.sizeof(WNDCLASSEXW), 0, self._wndproc, 0, 0, hinst, None,
                         user32.LoadCursorW(None, 32512), None, None, "BlobGlass", None)
        user32.RegisterClassExW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0x80000 | 0x80 | 0x8, "BlobGlass", APP_NAME, 0x80000000,
                                           0, 0, 10, 10, None, None, hinst, None)
        # keep our own pixels out of the background grab (otherwise the glass refracts itself)
        user32.SetWindowDisplayAffinity(self.hwnd, 0 if self.captureable else 0x11)
        self.glass.dump_path = os.environ.get("BLOB_DUMP")
        self.cur_hand = user32.LoadCursorW(None, 32649)
        self.cur_arrow = user32.LoadCursorW(None, 32512)

        self.icon = pystray.Icon(APP_NAME, tray_image(None), APP_NAME, menu=pystray.Menu(
            pystray.MenuItem("Open", lambda: user32.PostMessageW(self.hwnd, WM_APP_TOGGLE, 0, 0),
                             default=True, visible=False),
            pystray.MenuItem("Start with Windows", lambda i, it: set_startup(not startup_enabled()),
                             checked=lambda i: startup_enabled()),
            pystray.MenuItem("Exit", lambda: user32.PostMessageW(self.hwnd, WM_APP_EXIT, 0, 0)),
        ))
        self.icon.run_detached()
        user32.SetTimer(self.hwnd, 2, 1000, None)  # data / tray refresh
        if "--startup" not in sys.argv:
            time.sleep(1.2)
            self.show()

    # ── content ──
    def draw_content(self, crossfade=False):
        t0 = time.perf_counter()
        self._draw_content(crossfade)
        if PROFILE:
            PROFILE.setdefault("draw_content", []).append(time.perf_counter() - t0)

    def _draw_content(self, crossfade=False):
        ink, accent, controls = self.panel.draw(self.snap, self.sound, self.glassiness, self.startup,
                                                self.pointer_style, self.media, self.seek_value,
                                                self.captureable)
        pic = self.panel.pic
        if crossfade:
            # the tab bar is shared by every page, so its thumb slides instead of fading
            self.old_controls = [c for c in self.controls if not (c[0] == "seg" and c[1] == "page")]
            self.glass.set_content(ink, accent, pic)
            fade = self.springs.get("fade", 1.0, k=170, zeta=1.0)
            fade.x, fade.v, fade.target = 0.0, 0.0, 1.0
        else:
            self.glass.replace_content(ink, accent, pic)
        self.controls = controls
        h = self.springs.get("height", self.panel.height(self.snap), k=260, zeta=0.74)
        h.target = self.panel.height(self.snap)
        self.frame_dirty = True

    def set_compact(self, compact):
        cfg = engine.load_config()
        cfg["compact"] = compact
        engine.save_config(cfg)
        self.panel.set_compact(compact)
        self.old_page_springs()
        self.springs.pop("height", None)

    def set_captureable(self, on):
        """On: the panel shows up in screen recordings, but the glass freezes (it would otherwise
        refract its own reflection). Off: live glass, invisible to capture."""
        self.captureable = on
        cfg = engine.load_config()
        cfg["captureable"] = on
        engine.save_config(cfg)
        user32.SetWindowDisplayAffinity(self.hwnd, 0 if on else 0x11)
        self.glass.source.frozen = on
        if on:
            self.refresh_backdrop()
        self.frame_dirty = True

    def refresh_backdrop(self):
        """Grab one clean frame of what's behind the panel while it's briefly hidden."""
        if not self.captureable:
            return
        was = self.visible
        if was:
            user32.ShowWindow(self.hwnd, 0)
        self.glass.source.frozen = False
        self.glass.capture(self.pos[0], self.pos[1], self.panel.w / self.ss,
                           self.springs["height"].x / self.ss, force=True)
        self.glass.source.frozen = True
        if was:
            user32.ShowWindow(self.hwnd, 4)   # SW_SHOWNOACTIVATE
        self.frame_dirty = True

    # ── visibility ──
    def show(self):
        self.snap = self.mon.snapshot() or self.snap
        if not self.snap:
            return
        self.visible = True
        self.draw_content()
        h = self.springs["height"]
        h.x = h.target
        if not self.pinned or self.pos is None:
            cx, cy = cursor_pos()
            work, _ = work_area_at(cx, cy)
            m = round(12 * self.S)
            self.pos = [work.right - self.glass.W + self.glass.sp - m,
                        work.bottom - self.glass.H + self.glass.sp - m]  # bottom-right of the work area
        self.last_frame = time.perf_counter()
        self.frame_dirty = True
        if self.captureable:
            self.refresh_backdrop()
        self.frame()
        user32.ShowWindow(self.hwnd, 5)
        user32.SetForegroundWindow(self.hwnd)
        self._schedule()

    def hide(self):
        self.mouse_in = False
        self.hover = None
        user32.KillTimer(self.hwnd, 1)
        self.interval = 0
        user32.ShowWindow(self.hwnd, 0)
        self.visible = False
        self.last_hide = time.time()

    def _schedule(self, animating=True):
        want = 8  # poll the screen at ~120 Hz; frames are only drawn when something changed
        if want != self.interval:
            self.interval = want
            user32.SetTimer(self.hwnd, 1, want, None)

    # ── lenses from controls + springs ──
    def resolve(self, controls, presence, prefix):
        S, sp = self.S, self.springs
        out, viz = [], None
        for c in controls:
            kind = c[0]
            if kind == "static":
                L = dict(c[1])
                self._fade(L, presence)
                out.append(L)
            elif kind == "hover":
                _, key, rect, r = c
                h = sp.get(prefix + "hover:" + key, 0.0, k=420, zeta=1.0)
                live = (self.attached == key) if self.pointer_style != "system" else (self.hover == key)
                h.target = 1.0 if live and not prefix else 0.0
                a = h.x * presence
                if a > 0.01 or (not prefix and key in (self.attached, self.detaching)):
                    ox, oy = self.pull_offset(key, rect) if not prefix else (0.0, 0.0)
                    sx, sy = abs(ox) * 0.15, abs(oy) * 0.15  # a little stretch along the pull
                    out.append(dict(key=None if prefix else key,
                                    rect=(rect[0] + ox - sx, rect[1] + oy - sy, rect[2] + ox + sx, rect[3] + oy + sy),
                                    r=r, strength=5 * S * a, bevel=8 * S, zoom=1 - 0.04 * a,
                                    rim=0.5 * max(a, 0.2), frost=a, lift=0.08 * a, raised=0.6 * a))
            elif kind == "seg":
                _, key, (tx0, ty0, tx1, ty1), segs, sel = c
                inset = 3 * S
                a0, a1 = segs[sel]
                cx = sp.get(prefix + "segx:" + key, (a0 + a1) / 2, k=330, zeta=0.62)
                wd = sp.get(prefix + "segw:" + key, a1 - a0, k=330, zeta=0.7)
                cx.target, wd.target = (a0 + a1) / 2, a1 - a0
                m = min(1.0, abs(cx.v) / (700 * S))                 # how fast it's gliding
                stretch = min(0.5, abs(cx.v) / (1300 * S))
                press = sp.get(prefix + "segp:" + key, 0.0, k=500, zeta=0.6)
                press.target = 1.0 if self.pressed and self.pressed.startswith(key + ":") else 0.0
                half_w = (wd.x - 2 * inset) / 2 * (1 + stretch) * (1 + 0.06 * press.x)
                half_h = ((ty1 - ty0) / 2 - inset) * (1 - 0.3 * stretch) * (1 + 0.12 * press.x)
                cy = (ty0 + ty1) / 2
                L = dict(rect=(cx.x - half_w, cy - half_h, cx.x + half_w, cy + half_h), r=half_h,
                         strength=(10 + 10 * m + 6 * press.x) * S, bevel=11 * S, zoom=0.9 - 0.1 * m - 0.05 * press.x,
                         rim=1.0, frost=1.0 - 0.75 * max(m, press.x), lift=0.16 * (1 - 0.5 * m), raised=1.0)
                self._fade(L, presence)
                out.append(L)
            elif kind == "slider":
                _, key, x0, x1, cy, value = c
                live = self.sound_value(key) if self.slider_drag == key else value
                kx = sp.get(prefix + "sx:" + key, x0 + (x1 - x0) * live, k=1100, zeta=0.72)
                kx.target = x0 + (x1 - x0) * live
                press = sp.get(prefix + "sp:" + key, 0.0, k=420, zeta=0.55)
                press.target = 1.0 if self.slider_drag == key else 0.3 if self.hover == "slider:" + key else 0.0
                p = press.x
                stretch = min(0.4, abs(kx.v) / (2400 * S))
                th = 6 * S
                fill = dict(rect=(x0, cy - th / 2, max(x0 + th, kx.x), cy + th / 2), r=th / 2, fill=0.8)
                kr = 12 * S * (1 + 0.5 * p)
                knob = dict(rect=(kx.x - kr * (1 + stretch), cy - kr * (1 - 0.25 * stretch),
                                  kx.x + kr * (1 + stretch), cy + kr * (1 - 0.25 * stretch)),
                            r=kr, strength=(9 + 12 * p) * S, bevel=kr, zoom=0.82 - 0.14 * p, rim=1.0,
                            frost=0.35 * (1 - p), lift=0.24 * (1 - 0.7 * p), raised=1.0 - 0.5 * p)
                for L in (fill, knob):
                    self._fade(L, presence)
                    out.append(L)
            elif kind == "toggle":
                _, key, (x0, y0, x1, y1), on = c
                pos = sp.get(prefix + "tp:" + key, 1.0 if on else 0.0, k=280, zeta=0.52)
                pos.target = 1.0 if on else 0.0
                press = sp.get(prefix + "tq:" + key, 0.0, k=500, zeta=0.6)
                press.target = 1.0 if self.pressed == "toggle:" + key else 0.35 if self.hover == "toggle:" + key else 0.0
                h = y1 - y0
                g = max(0.0, min(1.0, pos.x))
                track = dict(rect=(x0, y0, x1, y1), r=h / 2, strength=5 * S, bevel=8 * S, rim=0.6,
                             frost=1.0 - g, lift=0.05 * (1 - g), tint=(52 / 255, 199 / 255, 89 / 255, 0.92 * g))
                kr = h / 2 - 3 * S
                stretch = min(0.55, abs(pos.v) / 7.0) + 0.25 * press.x
                left, right = x0 + 3 * S + kr, x1 - 3 * S - kr
                kx = left + (right - left) * pos.x
                ky = (y0 + y1) / 2
                grow = 1 + 0.18 * press.x
                knob = dict(rect=(kx - kr * (1 + stretch) * grow, ky - kr * grow, kx + kr * (1 + stretch) * grow,
                                  ky + kr * grow), r=kr * grow, strength=(7 + 8 * press.x) * S, bevel=kr,
                            zoom=0.85 - 0.1 * press.x, rim=1.0, frost=0.3 * (1 - press.x),
                            lift=0.45 * (1 - 0.6 * press.x), raised=1.0)
                for L in (track, knob):
                    self._fade(L, presence)
                    out.append(L)
            elif kind == "viz":
                viz = c[1]
        return out, viz

    @staticmethod
    def _fade(L, a):
        if a >= 0.999:
            return
        for k in ("strength", "rim", "frost", "lift", "raised", "fill"):
            if k in L:
                L[k] *= a
        if "tint" in L:
            t = L["tint"]
            L["tint"] = (t[0], t[1], t[2], t[3] * a)

    def sound_value(self, key):
        if key == "glass":
            return self.glassiness
        if key == "seek":
            m = self.media
            return self.seek_value if self.seek_value is not None else (m.pos_now() / m.duration if m.duration else 0)
        if key == "volume":
            return self.sound.volume if self.sound.volume is not None else 0.5
        return getattr(self.sound, key)

    # ── drawing ──
    def frame(self):
        t0 = time.perf_counter()
        self._frame()
        if PROFILE:
            PROFILE.setdefault("frame", []).append(time.perf_counter() - t0)
            for k, v in getattr(self.glass, "stats", {}).items():
                PROFILE.setdefault(k, []).append(v)
            self.glass.stats = {}
            if time.time() - PROFILE.setdefault("_t", time.time()) > 5:
                PROFILE["_t"] = time.time()
                parts = {k: (1000 * sum(v) / len(v), 1000 * max(v), len(v)) for k, v in PROFILE.items()
                         if k != "_t" and v}
                engine.log("profile " + ", ".join(f"{k}: avg {a:.1f}ms max {m:.1f}ms n={n}" for k, (a, m, n)
                                                    in parts.items()))
                for k in parts:
                    PROFILE[k] = []

    def _frame(self):
        now = time.perf_counter()
        dt = min(0.05, now - self.last_frame)
        self.last_frame = now
        self.springs.step(dt)
        fade = self.springs.get("fade", 1.0).x
        if self.panel.page == "music" and self.panel.music_view == "queue" and not self.am.queue                 and now - self.last_queue > 3:
            self.last_queue = now
            self.am.refresh_queue()
        if self.panel.page == "music" and self.panel.music_view == "search":
            key, since = self.hover_since
            if self.hover != key:
                self.hover_since = (self.hover, now)
            elif key and key.startswith("result:") and since and now - since > 0.35:
                self.am.prefetch(int(key.split(":")[1]))
                self.hover_since = (key, 0.0)  # once per rest
        if self.panel.page == "music":
            m = self.media
            seen = (m.art_version, m.playing, m.active, m.title)
            view = self.panel.music_view
            live = (view in ("now", "art") and m.playing) or view == "search"  # progress / caret
            if seen != self.media_seen or self.am.version != self.am_seen or \
                    (live and now - self.last_text > (0.25 if view == "now" else 0.5)):
                if seen != self.media_seen:
                    self.media_seen = seen
                    self.glass.set_ambient(*album_tint(m.art))
                self.am_seen = self.am.version
                self.last_text = now
                self.draw_content()
        viz_live = self.panel.page == "sound" and (self.sound.enabled or self.sound.spectrum.max() > 0.01)
        animating = self.springs.moving or self.drag is not None or bool(self.slider_drag)             or abs(self.vel).max() > 0.5
        force = animating or viz_live or self.frame_dirty
        # cheap path: poll the screen; draw only if the background or anything on the panel changed
        if not self.glass.capture(self.pos[0], self.pos[1], self.panel.w / self.ss,
                                  self.springs["height"].x / self.ss, force):
            return
        self.frame_dirty = False
        tr = time.perf_counter()
        lenses, viz = self.resolve(self.controls, fade, "")
        if fade < 0.999 and self.old_controls:
            old, _ = self.resolve(self.old_controls, 1.0 - fade, "old:")
            lenses = old + lenses
        k = 1.0 / self.ss
        for L in lenses:
            L["rect"] = tuple(v * k for v in L["rect"])
            for key in ("r", "strength", "bevel"):
                if key in L:
                    L[key] *= k
        self.glass.set_lenses(lenses)
        self._update_pointer(lenses)
        if PROFILE:
            PROFILE.setdefault("resolve+lenses", []).append(time.perf_counter() - tr)
        if viz is not None:
            self.sound.heartbeat()
            self.glass.set_viz([v / self.ss for v in viz], self.sound.spectrum, fade)
        else:
            self.glass.set_viz(None, None, 0)
        # light follows motion a little, like a droplet catching the light as it slides
        self.vel *= 0.85
        target = np.array([-0.55, -0.83]) + np.clip(self.vel * 0.02, -0.6, 0.6)
        self.light += (target - self.light) * 0.25
        self.light /= np.linalg.norm(self.light) + 1e-6
        self.light = np.round(self.light, 3)
        self.glass.render(self.hwnd, self.pos[0], self.pos[1], self.panel.w / self.ss,
                          self.springs["height"].x / self.ss, fade, tuple(self.light), self.glassiness)
        if fade >= 0.999 and self.old_controls:
            self.old_controls = []
            for k in [k for k in self.springs if k.startswith("old:")]:
                del self.springs[k]

    # ── glass pointer: fuses into what it hovers with surface tension, pulls, then snaps free ──
    SNAP = 15  # px the pointer can drag a button's glass before the bridge breaks

    def _update_pointer(self, lenses):
        S, sp = self.S, self.springs
        style = self.pointer_style
        hv = {c[1]: c[2] for c in self.controls if c[0] == "hover"}
        xy = self.mouse_xy if self.mouse_in else None
        if style == "system" or xy is None:
            if self.attached:
                self.detaching, self.attached = self.attached, None
        else:
            x, y = xy
            over = self.hover if self.hover in hv else None
            if over and over != self.attached:
                if self.attached:
                    self.detaching = self.attached
                self.attached = over
            elif self.attached and not over:
                rect = hv.get(self.attached)
                gap = rect_gap(x, y, rect) if rect else 1e9
                if gap > self.SNAP * S or self.hover:  # stretched too far (or onto another control): snap
                    self.detaching, self.attached = self.attached, None
        # surface-tension radius: big enough to keep a liquid neck across the gap while attached
        k = sp.get("ptr:k", 0.0, k=1400, zeta=0.85)
        if self.attached and xy:
            rect = hv.get(self.attached)
            gap = rect_gap(xy[0], xy[1], rect) if rect else 0
            k.target = max(6 * S, 1.05 * gap + 4 * S)
        else:
            k.target = 0.0
        if not self.attached and k.x < 0.6:
            self.detaching = None
        fuse = self.attached or self.detaching
        target = next((i for i, L in enumerate(lenses) if L.get("key") == fuse), -1) if fuse else -1
        a = sp.get("ptr:a", 0.0, k=700, zeta=1.0)
        on_other = self.hover and self.hover not in hv  # sliders / toggles: their knob reacts instead
        a.target = 1.0 if (xy and style != "system" and not on_other and not self.slider_drag) else 0.0
        shape = {"triangle": 0, "arrow": 1, "droplet": 2}.get(style, 1)
        self.glass.set_pointer([v / self.ss for v in xy] if xy else None, a.x, target,
                               max(0.0, k.x) / self.ss, shape)

    def pull_offset(self, key, rect):
        """Where a hover pill's glass is being dragged by the fused pointer (springs = wobble)."""
        S, sp = self.S, self.springs
        ox = sp.get("pull:x:" + key, 0.0, k=700, zeta=0.62)
        oy = sp.get("pull:y:" + key, 0.0, k=700, zeta=0.62)
        if key == self.attached and self.mouse_xy:
            x, y = self.mouse_xy
            cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
            vx, vy = x - cx, y - cy
            gap = rect_gap(x, y, rect)
            n = max(1e-3, (vx * vx + vy * vy) ** 0.5)
            pull = min(gap, self.SNAP * S) * 0.22
            ox.target = vx * 0.05 + vx / n * pull
            oy.target = vy * 0.05 + vy / n * pull
        else:
            ox.target = oy.target = 0.0
        return ox.x, oy.x

    def panel_local(self, lp):
        """Mouse position in layout units (the panel is drawn supersampled)."""
        h = self.springs["height"].x / self.ss
        x = ctypes.c_short(lp & 0xFFFF).value - self.glass.panel_x(self.panel.w / self.ss)
        y = ctypes.c_short((lp >> 16) & 0xFFFF).value - self.glass.panel_y(int(round(h)))
        return x * self.ss, y * self.ss

    def set_slider(self, key, v):
        v = max(0.0, min(1.0, v))
        if key == "seek":
            self.seek_value = v
            return
        if key == "volume":
            self.sound.set_system_volume(v)
            return
        if key == "glass":
            self.glassiness = v
            self.frame_dirty = True
        else:
            self.sound.set(key, v)

    def click(self, h, x):
        kind, _, key = h.partition(":")
        crossfade = False
        if kind == "page":
            if key != self.panel.page:
                self.glass.set_ambient(*(album_tint(self.media.art) if key == "music" else ((0, 0, 0, 0),)))
                self.media_seen = None
                self.old_page_springs()
                self.panel.page = key
                crossfade = True
                if key == "sound":
                    self.sound.refresh_devices()
                self.startup = startup_enabled()
        elif kind == "mode":
            self.mon.controller.set_mode(key)
            self.snap = self.mon.snapshot()
            self.snap["control"] = dict(self.snap["control"], mode=key)
        elif kind == "details":
            self.panel.details_open = not self.panel.details_open
            crossfade = True
            self.old_page_springs()
        elif kind == "preset":
            self.sound.set_preset(key)
        elif kind == "mview":
            self.panel.music_view = key
            self.panel.scroll = {}
            crossfade = True
            self.old_page_springs()
            if key == "queue":
                self.am.refresh_queue()
        elif kind == "result":
            self.am.play_result(int(key))
            self.hold_until = time.time() + 10
        elif kind == "queue":
            self.am.play_queue(int(key))
            self.hold_until = time.time() + 10
        elif kind == "size":
            self.set_compact(key == "compact")
            crossfade = True
        elif kind == "searchbox":
            pass
        elif kind == "am":
            (self.am.toggle_shuffle if key == "shuffle" else self.am.cycle_repeat)()
            self.hold_until = time.time() + 6
        elif kind == "media":
            {"toggle": self.media.toggle, "next": self.media.next, "previous": self.media.previous,
             "open": self.media.open_apple_music}[key]()
        elif kind == "pointer":
            self.pointer_style = key
            cfg = engine.load_config()
            cfg["pointer"] = key
            engine.save_config(cfg)
        elif kind == "toggle":
            if key == "sound":
                self.sound.set_enabled(not self.sound.enabled)
            elif key == "startup":
                set_startup(not startup_enabled())
                self.startup = startup_enabled()
            elif key == "capture":
                self.set_captureable(not self.captureable)
        elif kind == "device":
            self.sound.next_output()
        elif kind == "fxquit":
            self.sound.quit_fxsound()
        elif kind == "slider":
            self.slider_drag = key
            self.set_slider(key, self.panel.slider_value(key, x))
            user32.SetCapture(self.hwnd)
        self.draw_content(crossfade)
        self.frame()

    def old_page_springs(self):
        """Keep the outgoing page's animation state so its glass can fade out in place."""
        for k in [k for k in self.springs if k.startswith("old:")]:
            del self.springs[k]
        for k in [k for k in self.springs if ":" in k and not k.endswith(":page") and "hover:" not in k]:
            self.springs["old:" + k] = self.springs.pop(k)

    # ── messages ──
    def wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TIMER and wp == 1 and self.visible:
                self.frame()
                return 0
            if msg == WM_TIMER and wp == 2:
                self.tick()
                return 0
            if msg == WM_TIMER and wp == 3:
                user32.KillTimer(hwnd, 3)
                self._replay_keys()
                return 0
            if msg == WM_APP_TOGGLE:
                if self.visible:
                    self.hide()
                elif time.time() - self.last_hide > 0.35:
                    self.show()
                return 0
            if msg == WM_APP_EXIT:
                self.quit()
                return 0
            if msg == WM_APP_REFOCUS:  # Apple Music briefly took focus: hand it back to the panel
                if self.visible:
                    user32.SetForegroundWindow(hwnd)
                return 0
            if msg == WM_ACTIVATE and (wp & 0xFFFF) == 0 and self.visible and not self.pinned \
                    and not self.slider_drag and time.time() > self.hold_until:
                self.hide()
                return 0
            if msg == WM_KEYDOWN and wp == 0x1B:
                if self.panel.page == "music" and self.panel.music_view != "now":
                    self.click("mview:now", 0)
                else:
                    self.hide()
                return 0
            if msg == 0x102 and self.panel.page == "music" and self.panel.music_view == "search":  # WM_CHAR
                ch = chr(wp)
                if ch == "\r":
                    self.am.search(self.panel.query)
                    self.panel.scroll = {}
                elif ch == "\b":
                    self.panel.query = self.panel.query[:-1]
                elif ch == "\x7f" or ch == "\x17":  # Ctrl+Backspace / Ctrl+W: clear
                    self.panel.query = ""
                elif ch.isprintable():
                    self.panel.query += ch
                self.draw_content()
                self.frame_dirty = True
                return 0
            if msg == 0x20A:  # WM_MOUSEWHEEL
                view = self.panel.music_view
                if self.panel.page == "music" and view in ("search", "playlists"):
                    step = -1 if ctypes.c_short(wp >> 16).value > 0 else 1
                    self.panel.scroll[view] = max(0, self.panel.scroll.get(view, 0) + step)
                    self.draw_content()
                    self.frame_dirty = True
                return 0
            if msg == WM_MOUSEMOVE:
                if self.slider_drag:
                    x, _ = self.panel_local(lp)
                    self.set_slider(self.slider_drag, self.panel.slider_value(self.slider_drag, x))
                    now = time.perf_counter()
                    if now - self.last_text > 0.08:  # value labels ~12 Hz; the glass still moves every frame
                        self.last_text = now
                        self.draw_content()
                    self._schedule(True)
                elif self.drag:
                    x, y = cursor_pos()
                    nx, ny = x - self.drag[0], y - self.drag[1]
                    if abs(nx - self.pos[0]) + abs(ny - self.pos[1]) > 0:
                        self.vel += (nx - self.pos[0], ny - self.pos[1])
                        self.pos = [nx, ny]
                        self.pinned = True
                        if time.perf_counter() - self.last_frame >= 0.008:
                            self.frame()
                else:
                    x, y = self.panel_local(lp)
                    if not self.mouse_in:
                        self.mouse_in = True
                        tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), 0x2, hwnd, 0)
                        user32.TrackMouseEvent(ctypes.byref(tme))
                    self.mouse_xy = (x, y)
                    self.hover = self.panel.hit(x, y)
                    if time.perf_counter() - self.last_frame >= 0.006:
                        self.frame_dirty = True
                        self.frame()
                return 0
            if msg == 0x2A3 and not os.environ.get("BLOB_NOLEAVE"):  # WM_MOUSELEAVE (debug: ignore)
                self.mouse_in = False
                self.hover = None
                return 0
            if msg == WM_LBUTTONDOWN:
                x, y = self.panel_local(lp)
                h = self.panel.hit(x, y)
                if h:
                    self.pressed = h
                    self.click(h, x)
                else:
                    cx, cy = cursor_pos()
                    self.drag = (cx - self.pos[0], cy - self.pos[1])
                    user32.SetCapture(hwnd)
                return 0
            if msg in (WM_LBUTTONUP, WM_CAPTURECHANGED):
                self.pressed = None
                if self.slider_drag:
                    key, self.slider_drag = self.slider_drag, None
                    if key == "glass":
                        cfg = engine.load_config()
                        cfg["glass"] = self.glassiness
                        engine.save_config(cfg)
                    elif key == "seek":
                        if self.seek_value is not None and self.media.duration:
                            self.media.seek(self.seek_value * self.media.duration)
                        self.seek_value = None
                        self.draw_content()
                    elif key != "volume":
                        self.sound.save()
                    user32.ReleaseCapture()
                    self.draw_content()
                if self.drag:
                    self.drag = None
                    user32.ReleaseCapture()
                    if self.captureable:
                        self.refresh_backdrop()
                self._schedule(True)
                return 0
            if msg == WM_SETCURSOR:
                p = wintypes.POINT(*cursor_pos())
                user32.ScreenToClient(hwnd, ctypes.byref(p))
                lpv = (p.x & 0xFFFF) | ((p.y & 0xFFFF) << 16)
                x, y = self.panel_local(lpv)
                w, h = self.panel.w, self.springs["height"].x
                inside = 0 <= x < w and 0 <= y < h
                glass_ptr = inside and self.pointer_style != "system"
                user32.SetCursor(None if glass_ptr else self.cur_arrow)  # the glass pointer takes over
                return 1
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            import traceback
            engine.log("wndproc: " + traceback.format_exc())
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def tick(self):
        t0 = time.perf_counter()
        self._tick()
        if PROFILE:
            PROFILE.setdefault("tick", []).append(time.perf_counter() - t0)

    def _tick(self):
        self._maybe_end_capture()
        self.sound.heartbeat()
        s = self.mon.snapshot()
        if not s:
            return
        t = s["cpu"]["temp"]
        if not self.snap or t != self.snap["cpu"]["temp"]:
            self.icon.icon = tray_image(t)
        g = s["gpu"]
        fans = " / ".join(f"{f['rpm']:,}" if f["rpm"] is not None else "–" for f in s["fans"])
        gpu_txt = "asleep" if g["state"] == "sleeping" else fmt_temp(g["temp"]) + "C"
        self.icon.title = f"CPU {fmt_temp(t)}C · GPU {gpu_txt}\nFans {fans} rpm"[:127]
        self.snap = s
        if self.visible and not self.slider_drag and self.springs.get("fade", 1.0).x >= 0.999:
            self.draw_content()
            self.frame()

    # ── screenshots ──
    # To refract live, the panel hides itself from screen capture. When a screenshot shortcut is
    # pressed we freeze the glass, make the panel capturable, wait one frame, then replay the keys.
    def _keyboard_hook(self, code, wp, lp):
        try:
            if code == 0 and wp in (0x100, 0x104, 0x101, 0x105) and self.visible:
                k = ctypes.cast(lp, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                down = wp in (0x100, 0x104)
                if not k.flags & 0x10:  # ignore our own injected keys
                    held = lambda vk: user32.GetAsyncKeyState(vk) & 0x8000
                    win = held(0x5B) or held(0x5C)
                    if k.vkCode == 0x2C or (k.vkCode == 0x53 and win and held(0x10)):
                        if down and self.pending_keys is None and time.time() > self.capture_until:
                            self.pending_keys = "prtsc" if k.vkCode == 0x2C else "snip"
                            if win:
                                send_keys([(0xE8, True), (0xE8, False)])  # stop Win from opening Start
                            self._show_to_capture()
                            user32.SetTimer(self.hwnd, 3, 90, None)
                            return 1
                        if self.pending_keys is not None:
                            return 1  # swallow the matching key-up / repeats until we replay
        except Exception:
            pass
        return user32.CallNextHookEx(None, code, wp, lp)

    def _show_to_capture(self):
        self.glass.source.frozen = True
        user32.SetWindowDisplayAffinity(self.hwnd, 0)
        self.capture_until = time.time() + 8.0
        self.frame_dirty = True
        self.frame()

    def _replay_keys(self):
        held = lambda vk: user32.GetAsyncKeyState(vk) & 0x8000
        if self.pending_keys == "prtsc":
            send_keys([(0x2C, True), (0x2C, False)])
        elif self.pending_keys == "snip":
            if (held(0x5B) or held(0x5C)) and held(0x10):
                send_keys([(0x53, True), (0x53, False)])
            else:
                send_keys([(0x5B, True), (0x10, True), (0x53, True), (0x53, False), (0x10, False), (0x5B, False)])
        self.pending_keys = None

    def _maybe_end_capture(self):
        late = time.time() - self.capture_until
        if self.capture_until and late > 0 and (late > 12 or not snipping_active()):
            self.capture_until = 0.0
            user32.SetWindowDisplayAffinity(self.hwnd, 0x11)
            self.glass.source.frozen = False
            self.frame_dirty = True

    def quit(self):
        if self.mon.controller.mode == "auto":
            self.mon.asus.set_mode("balanced")
        self.sound.shutdown()
        user32.UnhookWindowsHookEx(self._hook)
        ctypes.windll.winmm.timeEndPeriod(1)
        self.icon.stop()
        user32.DestroyWindow(self.hwnd)

    def run(self):
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    try:
        App().run()
    except Exception:
        import traceback
        engine.log("fatal: " + traceback.format_exc())
        raise
