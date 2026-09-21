"""Blob — tray app. The taskbar shows the CPU temperature; click it for a liquid-glass
panel with hardware, sound, music, gaming and settings views. Hardware monitoring is read-only.
Sound provides system-wide boost / EQ like FxSound, with a glass spectrum. Drag the panel to pin it
anywhere (e.g. over a game or video); click the tray number again to hide it."""
import ctypes
import os
import subprocess
import sys
import threading
import time
import webbrowser
import winreg
from ctypes import wintypes

import numpy as np
import pystray
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import engine
import glass
from applemusic import AppleMusic
from media import NowPlaying
from gaming import GamingMonitor
from sound import PRESET_ORDER, Sound
from reactive import AudioMotion
from glass import user32


def music_visualizer_bands(spectrum, peak):
    """Real DSP bins when available; otherwise an honest uniform amplitude meter."""
    bands = np.nan_to_num(np.asarray(spectrum, dtype=np.float32), nan=0, posinf=0, neginf=0)
    if bands.size == 28 and bands.max(initial=0) > .025:
        return np.clip(bands, 0, 1)
    peak = float(peak)
    peak = min(1.0, max(0.0, peak)) if np.isfinite(peak) else 0.0
    return np.full(28, np.sqrt(peak), dtype=np.float32)


APP_NAME = "Blob v3"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
FONTS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts")

k32 = ctypes.windll.kernel32

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
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetCapture.restype = wintypes.HWND
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]

WM_DESTROY, WM_ACTIVATE, WM_SETCURSOR, WM_KEYDOWN, WM_TIMER = 0x02, 0x06, 0x20, 0x100, 0x113
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_CAPTURECHANGED = 0x200, 0x201, 0x202, 0x215
WM_APP_TOGGLE, WM_APP_EXIT, WM_APP_REFOCUS = 0x8001, 0x8002, 0x8003
WM_APP_GAMING_LOCK, WM_HOTKEY, GAMING_HOTKEY = 0x8004, 0x312, 1
WS_EX_TRANSPARENT, WS_EX_NOACTIVATE = 0x20, 0x08000000
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
DEFAULT_HOTKEY = (MOD_CONTROL | MOD_ALT, ord("V"))


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


def parse_hotkey(value):
    """Return a safe RegisterHotKey (modifier mask, virtual key) pair."""
    try:
        mods, vk = int(value.get("mods", DEFAULT_HOTKEY[0])), int(value.get("vk", DEFAULT_HOTKEY[1]))
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_HOTKEY
    return (mods & 0xF, vk) if mods & 0xF and 8 <= vk <= 0xFE else DEFAULT_HOTKEY


def hotkey_label(mods, vk):
    parts = []
    for flag, name in ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win")):
        if mods & flag:
            parts.append(name)
    if ord("A") <= vk <= ord("Z") or ord("0") <= vk <= ord("9"):
        key = chr(vk)
    elif 0x70 <= vk <= 0x87:
        key = f"F{vk - 0x6F}"
    else:
        key = {0x20: "Space", 0x09: "Tab", 0x08: "Backspace", 0x2D: "Insert",
               0x2E: "Delete", 0x24: "Home", 0x23: "End", 0x21: "Page Up",
               0x22: "Page Down", 0x25: "Left", 0x26: "Up", 0x27: "Right",
               0x28: "Down", 0xBA: ";", 0xBB: "+", 0xBC: ",", 0xBD: "-",
               0xBE: ".", 0xBF: "/", 0xC0: "`", 0xDB: "[", 0xDC: "\\",
               0xDD: "]", 0xDE: "'"}.get(vk, f"Key {vk}")
    return " + ".join(parts + [key])


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
def fmt_temp(t):
    return "–" if t is None else f"{round(t)}°"


def fmt_rpm(r):
    return "–" if r is None else "Off" if r == 0 else f"{r:,} rpm"


def empty_snapshot():
    """Usable UI state while the first hardware scan is still running.

    Sensor discovery can take several seconds on a fresh Windows installation.  The
    panel must still open so onboarding and dependency guidance are reachable.
    """
    return {
        "t": time.time(),
        "device": {"model": "Windows PC", "cpu": "", "gpu": "", "igpu": ""},
        "caps": {"nvidia": False, "fans_readable": False},
        "cpu_source": "none",
        "cpu": {"temp": None, "load": None, "clock": None},
        "gpu": {"state": "unavailable", "temp": None, "load": None,
                "power": None, "clock": None, "stale": True},
        "fans": [], "sensors": [],
        "power": {"plugged": True, "battery": None, "secsleft": None},
        "control": {"mode": None, "active": None, "reason": ""},
    }


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
PAGES = [("blob", "System"), ("sound", "Sound"), ("music", "Music"), ("gaming", "Gaming"), ("settings", "Settings")]
HARDWARE_MODES = [("auto", "Auto"), ("quiet", "Quiet"), ("balanced", "Balanced"),
                  ("performance", "Turbo"), ("custom", "Custom")]
POINTERS = [("arrow", "Arrow"), ("triangle", "Triangle"), ("droplet", "Droplet"), ("system", "System")]


class Panel:
    """Lays out one page: draws text into an ink layer and describes the glass controls.
    Glass controls are resolved into lenses every frame by App (so they can animate)."""

    TOP = 56    # room taken by the tab bar (which sits at the bottom)
    CONTENT = 18  # content starts this far below the top edge
    WIDE, NARROW = 340, 248
    BUBBLE_W, BUBBLE_H = 104, 98
    GAMING_WIDE, GAMING_NARROW = 624, 560
    RADIUS = 34     # the pane's corner; anything inset by p gets RADIUS - p (concentric radii)
    SS = 2          # content is drawn at 2x and filtered down on the GPU
    DETAIL_ROWS = 8  # keep large sensor inventories inside the renderer; scroll the rest

    def __init__(self, S):
        self.S = S * self.SS      # every layout number below is in supersampled pixels
        self.f = Fonts(self.S)    # fonts scale with the supersampled layout too
        self.compact = False
        self.backdrop = False      # album cover behind the music page
        self.tabs_open = False     # the switcher expands when you reach for it
        self.tabs_t = 0.0          # animated 0..1 between resting pill and full set
        self.options = {}          # the small on/off settings, straight from the config file
        self.hw_note = ""          # what this machine can and cannot report
        self.game = {}
        self.w = round(self.WIDE * self.S)
        self.page = "blob"
        self.music_view = "now"
        self.music_menu = None       # contextual mini/cover utility: volume or options
        self.hardware_view = "card"
        self.gaming_view = "strip"
        self.hardware_mode = "auto"
        self.hardware_status = "Monitoring only · fan profiles are not connected"
        self.hotkey_label = hotkey_label(*DEFAULT_HOTKEY)
        self.hotkey_editing = False
        self.hotkey_error = ""
        self.hover_key = None      # supplied by App so artwork can reveal contextual controls
        self.query = ""
        self.scroll = {}
        self.am = None
        self.details_open = False
        self.rects = {}
        self.sliders = {}

    def set_compact(self, compact):
        self.compact = compact
        self.update_width()

    def update_width(self):
        if ((self.page == "blob" and self.hardware_view == "bubble") or
                (self.page == "music" and self.music_view == "bubble") or
                (self.page == "gaming" and self.gaming_view == "bubble")):
            width = self.BUBBLE_W
        elif self.page == "music" and self.music_view == "art":
            width = self.WIDE
        else:
            widths = (self.GAMING_WIDE, self.GAMING_NARROW) if self.page == "gaming" else (self.WIDE, self.NARROW)
            width = widths[bool(self.compact)]
        self.w = round(width * self.S)

    def height(self, s, page=None):
        page, c = page or self.page, self.compact
        if page == "blob" and self.hardware_view == "bubble":
            return round(self.BUBBLE_H * self.S)
        if page == "gaming":
            if self.gaming_view == "bubble":
                return round(self.BUBBLE_H * self.S)
            return round((86 + (56 if self.tabs_open or self.tabs_t > .04 else 0)) * self.S)
        if page == "music":
            v = self.music_view
            if v == "bubble":
                return round(self.BUBBLE_H * self.S)
            if v == "art":
                return self.w
            if v in ("search", "queue"):
                return round((self.TOP + 470) * self.S)
            return round((self.TOP + (160 if c else 526)) * self.S)
        if page == "sound":
            return round((self.TOP + (150 if c else 462)) * self.S)
        if page == "settings":
            return round((self.TOP + (300 if c else 420)) * self.S)
        if page == "blob":
            if c and not self.details_open:
                return round((150+self.TOP)*self.S)
            base = 210
            if self.details_open and s:
                base = 238 + min(self.DETAIL_ROWS, len(self.detail_rows(s))) * 24
            return round((base + self.TOP) * self.S)
        base = 152 if c else 292
        return round((base + self.TOP) * self.S)

    @property
    def pad_u(self):
        if self.page == "music" and self.music_view == "art":
            return 4
        return 14 if self.compact else 22

    @property
    def radius(self):
        return self.RADIUS * self.S

    def inner_radius(self, inset, cap=None):
        """Nested corners: outer radius = inner radius + padding."""
        r = max(2 * self.S, self.radius - inset)
        return min(r, cap) if cap else r

    def max_height(self):
        return round((self.TOP + 300 + 16 * 24) * self.S)

    def detail_rows(self, s):
        cpu, gpu = s["cpu"], s["gpu"]
        awake = gpu["state"] != "sleeping"
        rows = [("CPU load", f"{round(cpu['load'])}%" if cpu["load"] is not None else "–", None),
                ("CPU clock", f"{cpu['clock'] / 1000:.2f} GHz" if cpu["clock"] else "–", None),
                ("GPU load", f"{round(gpu['load'])}%" if gpu["load"] is not None and awake else "–", None),
                ("GPU power", f"{gpu['power']:.0f} W" if gpu["power"] is not None else "–", None)]
        for i, fan in enumerate(s.get("fans", [])):
            rows.append((fan.get("name") or f"Fan {i + 1}", fmt_rpm(fan.get("rpm")), None))
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
        # Later controls are visually on top (notably transport over expanded
        # artwork), so they also win hit testing.
        for key, (x0, y0, x1, y1) in reversed(self.rects.items()):
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

    def text_font(self, text, size, style="Regular"):
        if any(ord(c) > 0x2500 for c in text):
            return self.f.emoji(size)
        return self.f.get(size, style)

    def label(self, x, y, t, size, style="Regular", a=175, anchor="la", temp=None, icon=False):
        font = self.f.icon(size) if icon else self.text_font(t, size, style)
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

    def segmented(self, prefix, options, selected, box, font_size=13, running=None, alpha=1.0, hit=True):
        """Frosted capsule track; the thumb is animated by App (slides, stretches, settles)."""
        S = self.S
        tx0, ty0, tx1, ty1 = box
        th = ty1 - ty0
        self.static(box, th / 2, strength=6 * S, bevel=9 * S, rim=0.6, frost=1.0, lift=0.05)
        if alpha <= 0.02:
            return
        opts = [(o[0], o[1], o[2] if len(o) > 2 else 1.0) for o in options]
        total = sum(o[2] for o in opts)
        x, segs, sel = tx0, [], 0
        for i, (key, name, wgt) in enumerate(opts):
            sw = (tx1 - tx0) * wgt / total
            segs.append((x, x + sw))
            if hit:
                self.rects[f"{prefix}:{key}"] = (x, ty0, x + sw, ty1)
            on = key == selected
            if not on and hit:
                ins = 3 * S
                self.controls.append(("hover", f"{prefix}:{key}", (x + ins, ty0 + ins, x + sw - ins, ty1 - ins),
                                      th / 2 - ins))
            if on:
                sel = i
            cx = x + sw / 2
            icon = len(name) == 1 and ord(name) > 0xE000
            self.label(cx, ty0 + th / 2, name, 11 if icon else font_size, "Semibold Text" if on else "Regular",
                       int((255 if on else 175) * alpha), "mm", icon=icon)
            if running == key:
                r, cy = 2 * S, ty1 - 3 * S - 6 * S
                self.di.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
            x += sw
        self.controls.append(("seg", prefix, box, segs, sel, alpha))

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
        self.update_width()
        # the album backdrop goes down first, under everything else on the music page
        S, W = self.S, self.w
        H = self.height(s)
        self._begin(H)
        pad = self.pad_u * S
        if self.page == "blob" and self.hardware_view == "bubble":
            self._hardware_bubble()
            return self.ink, self.accent, self.controls
        if self.page == "gaming":
            self._gaming(s)
            return self.ink, self.accent, self.controls
        focus_art = self.page == "music" and self.music_view in ("art", "bubble")
        if not focus_art:
            # The switcher sits at the bottom of a bottom-anchored pane, so it
            # never moves between ordinary views. Expanded artwork owns the
            # whole surface and provides its own contextual collapse control.
            t = max(0.0, min(1.0, self.tabs_t))       # 0 = resting pill, 1 = full set
            y0, y1 = H - 50 * S, H - 16 * S
            label = dict((k, n) for k, n in PAGES)[self.page]
            half_rest = (34 + 4.5 * len(label)) * S
            lerp = lambda a, b: a + (b - a) * t
            box = (lerp(W / 2 - half_rest, pad), lerp(y0 + 4 * S, y0),
                   lerp(W / 2 + half_rest, W - pad), lerp(y1 - 4 * S, y1))
            self.segmented("page", PAGES, self.page, box, 11 if self.compact else 13, alpha=t,
                           hit=t > 0.55)
            if t < 0.995:                              # the resting pill's own label
                a = int(215 * (1 - t))
                self.label(W / 2 - 7 * S, (box[1] + box[3]) / 2, label,
                           11 if self.compact else 12, "Semibold Text", a, "mm")
                self.di.text((W / 2 + half_rest - 13 * S, (box[1] + box[3]) / 2), "\uE70E",
                             font=self.f.icon(8), fill=int(150 * (1 - t)), anchor="mm")
            if t < 0.55:
                self.rects["tabs"] = (box[0] - 6 * S, y0 - 6 * S, box[2] + 6 * S, y1 + 6 * S)
                self.controls.append(("hover", "tabs", box, (box[3] - box[1]) / 2))
        top = self.CONTENT * S
        if self.page == "music" and self.backdrop and media is not None and media.art is not None:
            self._backdrop(media.art, H)
        if self.page == "sound":
            (self._sound_compact if self.compact else self._sound)(snd, pad, top)
        elif self.page == "settings":
            self._settings(glassiness, startup, pointer, pad, top, captureable)
        elif self.page == "music":
            self._music(media, snd, seek, pad, top)
        else:
            (self._blob_compact if self.compact and not self.details_open else self._blob)(s, pad, top)
        return self.ink, self.accent, self.controls

    def _hardware_bubble(self):
        """Hardware mode at a glance: the body cycles safe presets; the satellite restores the card."""
        S = self.S
        main = (4 * S, 16 * S, 82 * S, 94 * S)
        restore = (58 * S, 1 * S, 101 * S, 44 * S)
        self.rects["hcycle"] = main
        self.controls.append(("hover", "hcycle", main, 39 * S))
        self.rects["hview:card"] = restore
        self.controls.append(("hover", "hview:card", restore, 21.5 * S))
        labels = dict(HARDWARE_MODES)
        letters = {"auto": "A", "quiet": "Q", "balanced": "B", "performance": "T", "custom": "C"}
        self.label(43 * S, 48 * S, letters.get(self.hardware_mode, "A"), 25,
                   "Semibold Display", 255, "mm")
        self.label(43 * S, 70 * S, labels.get(self.hardware_mode, "Auto").upper(), 9,
                   "Semibold Text", 190, "mm")

    def _gaming(self, s):
        """A single glass instrument strip, not a second dashboard over the game."""
        S, W, game = self.S, self.w, self.game
        if self.gaming_view == "bubble":
            return self._gaming_bubble()
        self.glass_button("tabs", 27 * S, 43 * S, 15 * S, "\uE700", 12, always=True)
        self.glass_button("gview:bubble", 27 * S, 72 * S, 10 * S, "\uE73F", 9)
        cpu, gpu = s.get("cpu", {}), s.get("gpu", {})
        pct = lambda v: f"{v:.0f}% load" if v is not None else "No load sensor"
        fps, ms = game.get("fps"), game.get("frame_ms")
        fans = [f.get("rpm") for f in s.get("fans", [])]
        rpm = lambda i: f"{fans[i] / 1000:.1f}k" if i < len(fans) and fans[i] is not None else "—"
        ram, ram_gb = game.get("ram_percent"), game.get("ram_gb")
        power = gpu.get("power")
        power_hint = "AC power" if s.get("power", {}).get("plugged") else "On battery"
        fps_hint = f"{ms:.1f} ms" if ms is not None else "Focus a game"
        fps_status = game.get("status", "")
        if fps is None and "sign out" in fps_status.lower():
            fps_hint = "Sign out once"
        elif fps is None and any(w in fps_status for w in ("permission", "installer", "Install", "unavailable", "Couldn't")):
            fps_hint = "Setup needed"
        if fps is None and "Capture stopped" in fps_status:
            fps_hint = "Retrying…"
        metrics = [
            ("FPS", f"{fps:.0f}" if fps is not None else "—", fps_hint, None),
            ("CPU", fmt_temp(cpu.get("temp")), pct(cpu.get("load")), cpu.get("temp")),
            ("GPU", fmt_temp(gpu.get("temp")), pct(gpu.get("load")), gpu.get("temp")),
            ("RAM", f"{ram:.0f}%" if ram is not None else "—", f"{ram_gb:.1f} GB" if ram_gb is not None else "System memory", None),
            ("FANS", rpm(0), f"{rpm(1)} rpm" if len(fans) > 1 else "RPM", None),
            ("GPU POWER", f"{power:.0f} W" if power is not None else "—", power_hint, None),
        ]
        left, step = 58 * S, (W - 76 * S) / len(metrics)
        for i, (name, value, hint, temp) in enumerate(metrics):
            x, width = left + i * step, step - 10 * S
            self.label(x, 17 * S, name, 10, "Semibold Text", 170)
            self.label(x, 30 * S, self.fit(value, 23, "Semibold Display", width), 23,
                       "Semibold Display", 255, temp=temp)
            self.label(x, 61 * S, self.fit(hint, 11, "Regular", width), 11, "Regular", 190)
        # The navigation stays tucked away until the menu is opened.
        if self.tabs_open or self.tabs_t > .04:
            H = self.height(s)
            self.segmented("page", PAGES, self.page, (18 * S, H - 50 * S, W - 18 * S, H - 16 * S),
                           13, alpha=max(.05, min(1, self.tabs_t)), hit=self.tabs_t > .55)

    def _gaming_bubble(self):
        S = self.S
        fps, ms = self.game.get("fps"), self.game.get("frame_ms")
        self.label(43*S, 43*S, f"{fps:.0f}" if fps is not None else "—", 24,
                   "Semibold Display", 255, "mm")
        self.label(43*S, 63*S, "FPS", 10, "Semibold Text", 195, "mm")
        self.label(43*S, 80*S, f"{ms:.1f} ms" if ms is not None else "— ms", 11,
                   "Regular", 235, "mm")
        self.hover_lens("gview:strip", (58*S, S, 101*S, 44*S), 21.5*S)

    def fit(self, text, size, style, max_w):
        """Ellipsize text to max_w pixels."""
        font = self.text_font(text, size, style)
        if font.getlength(text) <= max_w:
            return text
        if font.getlength("…") > max_w:
            return ""
        while text and font.getlength(text + "…") > max_w:
            text = text[:-1]
        return text.rstrip() + "…"

    def wrap(self, text, size, style, max_w):
        """Wrap without shrinking type, including device names with no spaces."""
        lines = []
        for paragraph in (text or "").splitlines():
            line = ""
            for word in paragraph.split():
                candidate = (line + " " + word).strip()
                if self.text_font(candidate, size, style).getlength(candidate) <= max_w:
                    line = candidate
                    continue
                if line:
                    lines.append(line)
                line = ""
                for char in word:
                    candidate = line + char
                    if line and self.text_font(candidate, size, style).getlength(candidate) > max_w:
                        lines.append(line)
                        line = char
                    else:
                        line = candidate
            if line:
                lines.append(line)
        return lines or [""]

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

    def _backdrop(self, art, H):
        """The cover, blurred and dimmed, filling the page behind the content."""
        W = self.w
        side = min(art.size)
        img = art.crop(((art.width - side) // 2, (art.height - side) // 2,
                        (art.width + side) // 2, (art.height + side) // 2))
        img = img.resize((max(W, H) // 6, max(W, H) // 6), Image.LANCZOS)
        img = img.filter(ImageFilter.GaussianBlur(6)).resize((W, H), Image.LANCZOS).convert("RGBA")
        img.putalpha(Image.fromarray((glass.shape_mask(W, H, self.radius) * 150).astype(np.uint8)))
        self.pic.alpha_composite(img, (0, 0))

    def _music(self, m, snd, seek, pad, top):
        view = self.music_view
        if view == "bubble":
            return self._music_bubble(m)
        if view == "search":
            return self._music_search(pad, top)
        if view == "queue":
            return self._music_queue(pad, top)
        if view == "art":
            return self._music_art(m, snd, pad, top, seek)
        if self.compact:
            return self._music_mini(m, snd, seek, pad, top)
        return self._music_card(m, snd, seek, pad, top)

    def _music_card(self, m, snd, seek, pad, top):
        """V1's cover-first player, with optional bubble and volume utilities."""
        S, W = self.S, self.w
        a = round(W - 2*pad)
        self._artwork(m, round(pad), round(top), a, self.inner_radius(pad), 44)
        ty = top + a + 18*S
        tw = W - 2*pad - 78*S
        self.label(pad, ty, self.fit(m.title if m and m.active else "Not playing", 18,
                   "Semibold Text", tw), 18, "Semibold Text", 255)
        sub = (m.artist or m.source) if m and m.active else "Search to start"
        self.label(pad, ty+26*S, self.fit(sub, 13, "Regular", tw), 13, "Regular", 190)
        self.glass_button("mview:search", W-pad-52*S, ty+18*S, 15*S, "\uE721", 11, always=True)
        self.glass_button("mview:queue", W-pad-15*S, ty+18*S, 15*S, "\uE8FD", 11, always=True)
        self._progress(m, seek, pad+12*S, W-pad-12*S, ty+66*S, times=True)
        cy = ty+124*S
        self._music_transport(m, cy, compact=False)
        self.slider("volume", snd.volume if snd.volume is not None else .5,
                    pad+30*S, W-pad-58*S, cy+54*S)
        self.glass_button("mview:bubble", W-pad-13*S, cy+54*S, 13*S, "\uE73F", 10, always=True)

    def _music_transport(self, m, cy, compact):
        S, W, pad = self.S, self.w, self.pad_u*self.S
        rep = getattr(self.am, "repeat", "off")
        side = 12*S if compact else 17*S
        offset = 51*S if compact else 74*S
        self.transport("am:shuffle", pad+12*S, cy, side, "shuffle", active=bool(getattr(self.am, "shuffle", False)))
        self.transport("media:previous", W/2-offset, cy, (15 if compact else 24)*S, "previous")
        self.transport("media:toggle", W/2, cy, (21 if compact else 32)*S,
                       "pause" if m and m.playing else "play", always=True)
        self.transport("media:next", W/2+offset, cy, (15 if compact else 24)*S, "next")
        self.transport("am:repeat", W-pad-12*S, cy, side,
                       "repeat_one" if rep == "one" else "repeat", active=rep != "off")

    def _music_mini(self, m, snd, seek, pad, top):
        """V1 proportions: square cover, clear seek line and filled transport."""
        S, W = self.S, self.w
        a = round(54*S)
        self._artwork(m, round(pad), round(top+2*S), a, 6*S, 20)
        tx, right = pad+a+12*S, W-pad-28*S
        self.label(tx, top+8*S, self.fit(m.title if m and m.active else "Not playing", 13,
                   "Semibold Text", right-tx), 13, "Semibold Text", 255)
        self.label(tx, top+26*S, self.fit((m.artist or m.source) if m and m.active else "Search to start",
                   11, "Regular", right-tx), 11, "Regular", 190)
        self.glass_button("mview:bubble", W-pad-11*S, top+13*S, 11*S, "\uE73F", 9)
        self.glass_button("mview:search", W-pad-50*S, top+52*S, 12*S, "\uE721", 10)
        self.glass_button("mview:queue", W-pad-16*S, top+52*S, 12*S, "\uE8FD", 10)
        self._progress(m, seek, pad+12*S, W-pad-12*S, top+82*S, times=False)
        self._music_transport(m, top+120*S, compact=True)


    def _music_utility_panel(self, snd, box, cy):
        """Inline utility surface shared by the mini card and cover overlay."""
        menu = self.music_menu
        if menu not in ("volume", "options"):
            return
        S = self.S
        x0, y0, x1, y1 = box
        self.static(box, (y1 - y0) / 2, strength=8 * S, bevel=10 * S, zoom=.96,
                    rim=.9, frost=1.0, lift=.16, raised=1.0)
        if menu == "volume":
            self.di.text((x0 + 18 * S, cy), "\uE993", font=self.f.icon(10), fill=190, anchor="mm")
            self.slider("volume", snd.volume if snd.volume is not None else .5,
                        x0 + 38 * S, x1 - 18 * S, cy)
            return
        centres = np.linspace(x0 + 20 * S, x1 - 20 * S, 5)
        self.glass_button("mview:search", centres[0], cy, 12 * S, "\uE721", 9)
        self.glass_button("mview:queue", centres[1], cy, 12 * S, "\uE8FD", 9)
        reactive = self.options.get("musicreactive", True)
        self.glass_button("toggle:musicreactive", centres[2], cy, 12 * S, "", 9, always=reactive)
        for dx, half_h in ((-5, 4), (0, 7), (5, 5)):
            self.ink_shape((centres[2] + (dx - 1) * S, cy - half_h * S,
                            centres[2] + (dx + 1) * S, cy + half_h * S), S, 245 if reactive else 155)
        self.transport("am:shuffle", centres[3], cy, 12 * S, "shuffle",
                       active=bool(getattr(self.am, "shuffle", False)))
        rep = getattr(self.am, "repeat", "off")
        self.transport("am:repeat", centres[4], cy, 12 * S,
                       "repeat_one" if rep == "one" else "repeat", active=rep != "off")

    def _music_bubble(self, m):
        """Gesture player: tap toggles, double-tap skips, hold goes to the previous track."""
        S = self.S
        main = (4 * S, 16 * S, 82 * S, 94 * S)
        restore = (58 * S, 1 * S, 101 * S, 44 * S)
        self.rects["mbubble:gesture"] = main
        self.controls.append(("hover", "mbubble:gesture", main, 39 * S))
        self.rects["mview:now"] = restore
        self.controls.append(("hover", "mview:now", restore, 21.5 * S))
        cx, cy, u = 43 * S, 55 * S, 10 * S
        if m and m.playing:
            bw, bh = 3.2 * S, 10 * S
            for sx in (-1, 1):
                x = cx + sx * 5 * S
                self.di.rounded_rectangle((x - bw / 2, cy - bh, x + bw / 2, cy + bh),
                                          radius=bw / 2, fill=235)
        else:
            self.di.polygon([(cx - u * .55, cy - u), (cx - u * .55, cy + u),
                             (cx + u * .95, cy)], fill=235)

    def _music_art(self, m, snd, pad, top, seek=None):
        """Expanded cover; hovering reveals an in-art now-playing overlay."""
        S, W = self.S, self.w
        a = round(W - 2 * pad)
        ax, ay = round(pad), round(pad)
        self._artwork(m, ax, ay, a, self.inner_radius(pad), 60)
        self.rects.pop("mview:now", None)
        artbox = (ax, ay, ax + a, ay + a)
        self.rects["arthover"] = artbox
        self.controls.append(("hover", "arthover", artbox, self.inner_radius(pad)))

        # The back control always has its own contrast, including white covers.
        cx, cy, r = ax+24*S, ay+24*S, 15*S
        ImageDraw.Draw(self.pic).ellipse((cx-r, cy-r, cx+r, cy+r), fill=(18, 18, 18, 225))
        self.glass_button("mview:now", cx, cy, r, "\uE72B", 11, always=True)

        # Audio stays inside the artwork; never scale the window or its controls.
        if self.options.get("musicreactive", True):
            self.controls.append(("viz", (ax + 56*S, ay + a - 194*S,
                                          ax + a - 56*S, ay + a - 168*S)))

        hover = self.hover_key or ""
        reveal = bool(self.music_menu) or hover == "arthover" or hover == "mview:now" or hover.startswith(
            ("media:", "am:", "musicutil:", "mview:", "slider:seek", "slider:volume"))
        if not reveal:
            return

        # A quiet lower gradient keeps metadata readable without obscuring the cover.
        band_h = round(158 * S)
        overlay = np.zeros((band_h, a, 4), np.uint8)
        overlay[..., :3] = 8
        gradient = np.linspace(20, 190, band_h, dtype=np.float32)[:, None]
        art_mask = glass.shape_mask(a, a, self.inner_radius(pad))[a - band_h:]
        overlay[..., 3] = np.clip(gradient * art_mask, 0, 255).astype(np.uint8)
        self.pic.alpha_composite(Image.fromarray(overlay, "RGBA"), (ax, ay + a - band_h))

        title = (m.title if m and m.active else None) or "Not playing"
        artist = (m.artist if m and m.active else None) or "Open search to choose something"
        self.label(W / 2, ay + a - 136 * S,
                   self.fit(title, 15, "Semibold Text", a - 70 * S),
                   15, "Semibold Text", 255, "ma")
        self.label(W / 2, ay + a - 114 * S,
                   self.fit(artist, 12, "Regular", a - 54 * S),
                   12, "Regular", 190, "ma")
        self._progress(m, seek, ax + 24 * S, ax + a - 24 * S, ay + a - 88 * S, times=False)
        cy = ay + a - 45 * S
        playing = bool(m and m.playing)
        if not self.music_menu:
            self.transport("media:previous", W / 2 - 56 * S, cy, 17 * S, "previous")
            self.transport("media:toggle", W / 2, cy, 23 * S, "pause" if playing else "play", always=True)
            self.transport("media:next", W / 2 + 56 * S, cy, 17 * S, "next")
        self._music_utility_panel(snd, (ax + 38 * S, cy - 25 * S, ax + a - 38 * S, cy + 25 * S), cy)
        self.glass_button("musicutil:volume", ax + 18 * S, cy, 13 * S, "\uE995", 10,
                          always=self.music_menu == "volume")
        self.glass_button("musicutil:options", ax + a - 18 * S, cy, 13 * S, "\uE712", 10,
                          always=self.music_menu == "options")

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
        key = "mview:" + ("now" if self.music_view == "art" else "art")
        self.rects[key] = (ax, ay, ax + a, ay + a)
        if self.music_view != "art" and self.hover_key == key:
            # The cover remains one large click target; the corner glyph only
            # explains what that click will do.
            r, cx, cy = 12 * self.S, ax + 16 * self.S, ay + 16 * self.S
            self.static((cx - r, cy - r, cx + r, cy + r), r, strength=6 * self.S,
                        bevel=7 * self.S, rim=.8, frost=.8, lift=.12, raised=1.0)
            self.di.text((cx, cy), "\uE8A7", font=self.f.icon(9), fill=245, anchor="mm")

    def _progress(self, m, seek, x0, x1, y, times=True):
        S = self.S
        dur = m.duration if (m and m.active) else 0.0
        pos = (seek * dur if seek is not None else m.pos_now()) if dur else 0.0
        frac = pos / dur if dur > 0 else 0.0
        if m and m.active and dur > 0:
            self.slider("seek", frac, x0, x1, y)
            if not times:
                self.rects["slider:seek"] = (x0 - 12 * S, y - 12 * S, x1 + 12 * S, y + 12 * S)
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
        r = 16 * S
        self.glass_button("mview:now", pad + r, top + 20 * S, r, "\uE72B", 11, always=True)
        if title:
            self.label(pad + 2 * r + 12 * S, top + 20 * S, title, 16, "Semibold Text",
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
        left = pad + 40 * S
        box = (left, top + 2 * S, W - pad, top + 38 * S)
        self.static(box, (box[3] - box[1]) / 2, strength=5 * S, bevel=9 * S, rim=0.8, frost=1.0, lift=0.07)
        self.rects["searchbox"] = box
        self.di.text((box[0] + 14 * S, (box[1] + box[3]) / 2), "\uE721", font=self.f.icon(10), fill=150, anchor="lm")
        tx = box[0] + 30 * S
        size = 14
        if self.query:
            shown = self.fit(self.query, size, "Regular", box[2] - tx - 14 * S)
            L(tx, (box[1] + box[3]) / 2, shown, size, "Regular", 255, "lm")
            cx = tx + self.f.get(size).getlength(shown) + 2 * S
        else:
            L(tx, (box[1] + box[3]) / 2,
              self.fit("Search Apple Music", size, "Regular", box[2] - tx - 14 * S),
              size, "Regular", 120, "lm")
            cx = tx
        if int(time.time() * 2) % 2 == 0:
            self.ink_shape((cx, box[1] + 10 * S, cx + 1.5 * S, box[3] - 10 * S), 0.75 * S, 230)
        row_h = self.ROW_SEARCH
        list_top, list_bottom = top + 50 * S, self.height(None) - 90 * S
        results = am.results if am else []
        for i, y in self._list_rows("search", len(results), row_h, list_top, list_bottom):
            r = results[i]
            row = (pad - 8 * S, y, W - pad + 8 * S, y + (row_h - 4) * S)
            self.rects[f"result:{i}"] = row
            self.controls.append(("hover", f"result:{i}", row, 13 * S))
            th = round(40 * S)
            if r.get("art") is not None:
                img = r["art"].resize((th, th), Image.LANCZOS).convert("RGBA")
                img.putalpha(Image.fromarray((glass.shape_mask(th, th, self.inner_radius(pad, th / 2)) * 255).astype(np.uint8)))
                self.pic.alpha_composite(img, (round(pad), round(y + 3 * S)))
            elif r.get("kind") == "playlist":
                self.di.text((pad + th / 2, y + 3 * S + th / 2), "\uE8FD", font=self.f.icon(12), fill=170,
                             anchor="mm")
            tx2 = pad + th + 12 * S
            tw = W - pad - tx2 - 8 * S
            L(tx2, y + 7 * S, self.fit(r["title"], 13, "Semibold Text", tw), 13,
              "Semibold Text", 255)
            sub = r["artist"] + ((" \u2014 " + r["album"]) if r.get("album") else "")
            L(tx2, y + 25 * S, self.fit(sub, 12, "Regular", tw), 12, "Regular", 165)
        msg = am.status if am else ""
        if not msg and not results:
            msg = "Type a song, artist or playlist, then Enter."
        L(pad, self.height(None) - 76 * S,
          self.fit(msg, 12, "Regular", W - 2 * pad), 12, "Regular", 165)

    def _music_queue(self, pad, top):
        S, W = self.S, self.w
        am, L = self.am, self.label
        self._back_row(pad, top, "Playing Next")
        items = am.queue if am else []
        row_h = 42
        list_top, list_bottom = top + 48 * S, self.height(None) - 90 * S
        if not items:
            msg = "Loading Playing Next…" if getattr(am, "queue_loading", False) else "Nothing queued up."
            L(pad, list_top + 10 * S, msg, 12, "Regular", 165)
        for i, y in self._list_rows("queue", len(items), row_h, list_top, list_bottom):
            q = items[i]
            row = (pad - 8 * S, y, W - pad + 8 * S, y + (row_h - 4) * S)
            self.rects[f"queue:{i}"] = row
            self.controls.append(("hover", f"queue:{i}", row, 12 * S))
            th = round(32 * S)
            if q.get("art") is not None and self.options.get("queueart", True):
                img = q["art"].resize((th, th), Image.LANCZOS).convert("RGBA")
                img.putalpha(Image.fromarray((glass.shape_mask(th, th, self.inner_radius(pad, th / 2)) * 255).astype(np.uint8)))
                self.pic.alpha_composite(img, (round(pad), round(y + 3 * S)))
            else:
                self.static((pad, y + 3 * S, pad + th, y + 3 * S + th), self.inner_radius(pad, th / 2),
                            frost=1.0, lift=0.08, n=5.0)
            tx = pad + th + 10 * S
            tw = W - pad - tx - 8 * S
            L(tx, y + 3 * S, self.fit(q["title"], 13, "Semibold Text", tw), 13,
              "Semibold Text", 235)
            L(tx, y + 20 * S, self.fit(q["artist"], 11, "Regular", tw), 11, "Regular", 160)
        L(pad, self.height(None) - 76 * S,
          self.fit((getattr(am, "queue_action_status", "") or getattr(am, "queue_status", am.status))
                   if am else "", 12, "Regular", W - 2 * pad), 12, "Regular", 165)

    def _volume_row(self, snd, pad, y):
        S, W = self.S, self.w
        self.di.text((pad + 6 * S, y), "\uE993", font=self.f.icon(12), fill=175, anchor="mm")
        self.di.text((W - pad - 6 * S, y), "\uE995", font=self.f.icon(12), fill=175, anchor="mm")
        self.slider("volume", snd.volume if snd.volume is not None else 0.5, pad + 30 * S, W - pad - 30 * S, y)

    def _blob_compact(self, s, pad, top):
        """Read-only hardware health, sized for a screen corner."""
        S, W = self.S, self.w
        px = lambda v: top + v * S
        L = self.label
        cpu, gpu, fans = s["cpu"], s["gpu"], s["fans"]
        col2 = W / 2 + 4 * S
        L(pad, px(2), "CPU", 11)
        L(pad, px(14), fmt_temp(cpu["temp"]), 30, "Semibold Display", 255 if cpu["temp"] else 105,
          temp=cpu["temp"])
        L(pad, px(54), ("Fan " + fmt_rpm(fans[0]["rpm"])) if fans else "no fan data", 11)
        asleep = gpu["state"] == "sleeping"
        L(col2, px(2), "GPU · off" if asleep else "GPU", 11)
        L(col2, px(14), "–" if asleep else fmt_temp(gpu["temp"]), 30, "Semibold Display",
          105 if asleep else 255, temp=None if asleep else gpu["temp"])
        if len(fans) > 1:
            L(col2, px(54), "Fan " + fmt_rpm(fans[1]["rpm"]), 11)
        self.di.rectangle((pad, px(80), W - pad, px(80) + max(1, round(S)) - 1), fill=45)
        source = {"hwmonitor": "LibreHardwareMonitor", "hwinfo": "HWiNFO",
                  "zone": "Windows ACPI"}.get(s.get("cpu_source"))
        L(pad, px(94), "Read-only hardware monitoring", 11, "Semibold Text", 200)
        note = f"Sensors via {source}" if source else "Run LibreHardwareMonitor for more sensors"
        L(pad, px(114), self.fit(note, 10, "Regular", W - 2 * pad - 36*S), 10, "Regular", 145)
        self.glass_button("hview:bubble", W-pad-13*S, px(116), 13*S, "\uE73F", 10)

    def _sound_compact(self, snd, pad, top):
        """On/off, boost and the spectrum — the rest lives in the regular size."""
        S, W = self.S, self.w
        px = lambda v: top + v * S
        L = self.label
        L(pad, px(12), "Boost", 13, "Semibold Text", 255, "lm")
        L(W - pad - 58 * S, px(12), f"+{snd.boost_db:.0f} dB", 12, "Regular", 175, "rm")
        self.toggle("sound", snd.enabled, W - pad - 48 * S, px(-2))
        self.slider("boost", snd.boost, pad + 12 * S, W - pad - 12 * S, px(48))
        if snd.cable is False:
            self.controls.append(("viz", (pad, px(70), W - pad, px(85))))
            bx = (pad, px(96), W - pad, px(126))
            self.rects["setupaudio"] = bx
            self.static(bx, 15 * S, strength=5 * S, bevel=8 * S, zoom=0.96,
                        rim=0.9, frost=1.0, lift=0.12, raised=1.0)
            L((bx[0] + bx[2]) / 2, px(111), "Set up system-wide audio", 11,
              "Semibold Text", 255, "mm")
        elif snd.error or snd.fx_conflict:
            self.controls.append(("viz", (pad, px(70), W - pad, px(85))))
            msg = "FxSound is using the audio." if snd.fx_conflict else snd.error
            for i, line in enumerate(self.wrap(msg, 11, "Regular", W - 2 * pad)[:2]):
                L(pad, px(92 + i * 15), line, 11)
        else:
            self.controls.append(("viz", (pad, px(70), W - pad, px(120))))

    def _blob(self, s, pad, top):
        S, W = self.S, self.w
        px = lambda v: top + v * S
        cpu, gpu, fans = s["cpu"], s["gpu"], s["fans"]
        col2 = W / 2 + 6 * S
        L = self.label
        # A single quiet lens is enough to suggest the circular bubble state.
        bubble_cx, bubble_cy, bubble_r = W - pad - 13 * S, px(13), 13 * S
        bubble_box = (bubble_cx - bubble_r, bubble_cy - bubble_r,
                      bubble_cx + bubble_r, bubble_cy + bubble_r)
        self.static(bubble_box, bubble_r, strength=5 * S, bevel=7 * S, zoom=0.94,
                    rim=0.8, frost=0.82, lift=0.11, raised=0.7, n=2.0)
        self.controls.append(("hover", "hview:bubble", bubble_box, bubble_r))
        self.rects["hview:bubble"] = bubble_box

        L(pad, px(4), "CPU", 11, "Semibold Text", 165)
        L(pad, px(20), fmt_temp(cpu["temp"]), 38, "Semibold Display",
          255 if cpu["temp"] is not None else 105, temp=cpu["temp"])
        fan0 = fmt_rpm(fans[0]["rpm"]) if fans else "No fan sensor"
        L(pad, px(66), fan0, 11, "Regular", 155)
        if gpu["state"] == "sleeping":
            L(col2, px(4), "GPU · asleep", 11, "Semibold Text", 165)
            L(col2, px(20), "–", 38, "Semibold Display", 105)
        else:
            active = gpu["state"] == "active"
            L(col2, px(4), "GPU" if active else "GPU · no sensor" if gpu["state"] == "unavailable"
              else "GPU · idle", 11, "Semibold Text", 165)
            L(col2, px(20), fmt_temp(gpu["temp"]), 38, "Semibold Display",
              255 if gpu["temp"] is not None else 105, temp=gpu["temp"])
        if len(fans) > 1:
            L(col2, px(66), fmt_rpm(fans[1]["rpm"]), 11, "Regular", 155)
        elif not fans and gpu["state"] == "unavailable":
            L(col2, px(66), "No fan sensor", 11, "Regular", 155)

        self.di.rectangle((pad, px(88), W - pad, px(88) + max(1, round(S)) - 1), fill=45)
        L(pad, px(104), "Hardware monitoring", 13, "Semibold Text", 205)
        mode_segments = [("auto", "Auto", .72), ("quiet", "Quiet", .82),
                         ("balanced", "Balanced", 1.15), ("performance", "Turbo", .82),
                         ("custom", "Custom", 1.0)]
        self.segmented("hmode", mode_segments, self.hardware_mode,
                       (pad, px(129), W - pad, px(159)), 11)
        status = self.fit(self.hardware_status, 11, "Regular", W - 2 * pad - 34 * S)
        L(pad, px(176), status, 11, "Regular", 140, "lm")

        rows = self.detail_rows(s) if self.details_open else []
        if self.details_open:
            self.di.rectangle((pad, px(196), W - pad, px(196) + max(1, round(S)) - 1), fill=45)
            bottom = px(216 + min(self.DETAIL_ROWS, len(rows)) * 24)
            for i, y in self._list_rows("details", len(rows), 24, px(216), bottom):
                k, v, t = rows[i]
                v = self.fit(v, 13, "Regular", (W - 2 * pad) * 0.60)
                name_w = W - 2 * pad - self.text_font(v, 13).getlength(v) - 12 * S
                L(pad, y, self.fit(k, 13, "Regular", name_w), 13)
                L(W - pad, y, v, 13, "Regular", 235, "ra", temp=t)

        # Share the resting tab pill's baseline, then yield the whole row as soon as that pill opens.
        if not self.tabs_open and self.tabs_t < .04:
            chevron_y = self.height(s) - 33 * S
            detail_box = (W - pad - 27 * S, chevron_y - 13 * S,
                          W - pad - 1 * S, chevron_y + 13 * S)
            self.hover_lens("details", detail_box, 13 * S)
            self.di.text((W - pad - 14 * S, chevron_y), "" if self.details_open else "",
                         font=self.f.icon(10), fill=190, anchor="mm")

    def _sound(self, snd, pad, top):
        S, W = self.S, self.w
        px = lambda v: top + v * S
        L = self.label
        L(pad, px(6), "Output", 12)
        self.hover_lens("device", (pad - 6 * S, px(20), W - pad - 64 * S, px(44)), 12 * S)
        L(pad, px(32), self.fit(short_device(snd.output) + "  ›", 14, "Semibold Text",
                              W - 2 * pad - 70 * S), 14, "Semibold Text", 255, "lm")
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
        if snd.cable is False:
            bx = (W - pad - 126 * S, px(414), W - pad, px(444))
            self.rects["setupaudio"] = bx
            self.static(bx, (bx[3] - bx[1]) / 2, strength=5 * S, bevel=8 * S,
                        zoom=0.95, rim=0.9, frost=1.0, lift=0.16, raised=1.0)
            L((bx[0] + bx[2]) / 2, px(429), "Set up audio", 12,
              "Semibold Text", 255, "mm")
            for i, line in enumerate(self.wrap("System-wide sound needs the free VB-Cable driver.",
                                               12, "Regular", bx[0] - pad - 12 * S)[:2]):
                L(pad, px(421 + i * 15), line, 12, anchor="lm")
            return
        if snd.fx_conflict:
            bx = (W - pad - 104 * S, px(416), W - pad, px(444))
            self.rects["fxquit"] = bx
            self.static(bx, (bx[3] - bx[1]) / 2, strength=5 * S, bevel=8 * S, zoom=0.95, rim=0.9, frost=1.0,
                        lift=0.16, raised=1.0)
            L((bx[0] + bx[2]) / 2, px(430), "Quit FxSound", 12, "Semibold Text", 255, "mm")
            for i, line in enumerate(self.wrap("FxSound is using the audio.", 12, "Regular",
                                               bx[0] - pad - 12 * S)[:2]):
                L(pad, px(422 + i * 15), line, 12, anchor="lm")
            return
        if snd.error:
            msg = snd.error
        elif snd.enabled:
            msg = f"Enhancing everything you hear · +{snd.boost_db:.0f} dB boost"
        else:
            msg = "Off · turn on to boost and shape your audio"
        for i, line in enumerate(self.wrap(msg, 12, "Regular", W - 2 * pad)[:2]):
            L(pad, px(422 + i * 15), line, 12, anchor="lm")

    def _settings(self, glassiness, startup, pointer, pad, top, captureable=False):
        """Grouped, scrollable settings: each section covers one part of Blob."""
        S, W = self.S, self.w
        c = self.compact
        L = self.label
        opts = self.options
        rows = [
            ("head", "Appearance"),
            ("seg", "size", "Size", [("normal", "Regular"), ("compact", "Compact")],
             "compact" if c else "normal"),
            ("slider", "glass", "Glass", glassiness, "Clear" if glassiness < 0.08 else "Frosted"
             if glassiness > 0.85 else f"{round(glassiness * 100)}% frosted"),
            ("seg", "pointer", "Pointer", POINTERS, pointer),
            ("toggle", "capture", "Include Blob in screenshots and recordings", captureable,
             "Glass freezes while included to avoid capturing itself"),
            ("head", "Music"),
            ("toggle", "backdrop", "Album backdrop", self.backdrop, "The cover, blurred, behind the page"),
            ("toggle", "queueart", "Covers in Playing Next", opts.get("queueart", True), None),
            ("toggle", "musicreactive", "Reactive music", opts.get("musicreactive", True),
             "Reactive bubble and in-cover visualizer; the cover stays still"),
            ("note", "music_bubble", "Music bubble: tap to play or pause, double-tap for next, hold for previous.", None, None),
            ("head", "Sound"),
            ("toggle", "soundstart", "Turn boost on at launch", opts.get("soundstart", False), None),
            ("head", "Hardware"),
            ("note", "hardware", self.hw_note, None, None),
            ("head", "System"),
            ("toggle", "startup", "Start with Windows", startup, None),
        ]
        rows[rows.index(("head", "Hardware")):rows.index(("head", "Hardware"))] = [
            ("head", "Overlay"),
            ("seg", "gview", "Gaming view", [("strip", "Strip"), ("bubble", "FPS bubble")], self.gaming_view),
            ("note", "overlay_info", "One lock state is shared by every tab and only changes when you use the shortcut or tray command.", None, None),
            ("note", "gaming_info", self.game.get("status", "Open Gaming for FPS, frame time and system stats."), None, None),
            ("note", "gaming_tip", f"Blob stays unlocked across tabs until you use {self.hotkey_label} to lock it. While locked it passes clicks through; in Gaming, hold the shortcut modifiers to drag temporarily.", None, None),
            ("keybind", "overlay", "Overlay lock shortcut", self.hotkey_label,
             self.hotkey_error or "Click the shortcut, then press a modified key combination"),
        ]
        h = {"head": 30, "slider": 62, "seg": 58, "toggle": 40, "note": 46, "keybind": 58}
        layouts = []
        for row in rows:
            kind = row[0]
            names, hints = [], []
            rh = h[kind]
            if kind == "toggle":
                names = self.wrap(row[2], 13 if c else 14, "Regular", W - 2 * pad - 60 * S)
                hints = self.wrap(row[4], 10, "Regular", W - 2 * pad) if row[4] else []
                rh = max(40, 22 + len(names) * 18 + len(hints) * 14)
            elif kind == "note":
                names = self.wrap(row[2], 11, "Regular", W - 2 * pad)
                rh = max(30, 14 + len(names) * 16)
            elif kind == "keybind":
                names = self.wrap(row[2], 13 if c else 14, "Regular", W - 2 * pad)
                hints = self.wrap(row[4], 10, "Regular", W - 2 * pad)
                rh = 72 + len(names) * 18 + len(hints) * 14
            layouts.append((row, rh * S, names, hints))
        total = sum(rh for _, rh, _, _ in layouts)
        view_h = self.height(None) - top - 78 * S
        # Scroll by complete variable-height rows. Every control can become fully visible,
        # with no half-hidden hit targets or fixed 40px steps skipping wrapped rows.
        last_first, tail_h = len(layouts) - 1, layouts[-1][1]
        while last_first > 0 and tail_h + layouts[last_first - 1][1] <= view_h:
            last_first -= 1
            tail_h += layouts[last_first][1]
        first = max(0, min(int(self.scroll.get("settings", 0)), last_first))
        self.scroll["settings"] = first
        off = sum(rh for _, rh, _, _ in layouts[:first])
        max_off = sum(rh for _, rh, _, _ in layouts[:last_first])
        y = top
        for index, (row, rh, names, hints) in enumerate(layouts[first:], first):
            kind, key = row[0], row[1]
            if kind == "head" and index + 1 < len(layouts) and y + rh + layouts[index + 1][1] > top + view_h:
                break  # keep each section title with its first setting
            if y + rh <= top + view_h:
                if kind == "head":
                    L(pad, y + 14 * S, key.upper(), 10, "Semibold Text", 195, "lm")
                    self.di.rectangle((pad, y + 24 * S, W - pad, y + 24 * S + max(1, round(S)) - 1), fill=32)
                elif kind == "note":
                    for i, line in enumerate(names):
                        L(pad, y + (10 + i * 16) * S, line, 11, "Regular", 205, "lm")
                elif kind == "slider":
                    _, _, name, val, hint = row
                    L(pad, y + 10 * S, name, 13 if c else 14, "Semibold Text", 255, "lm")
                    L(W - pad, y + 10 * S, hint, 12, "Regular", 165, "rm")
                    self.slider(key, val, pad + 12 * S, W - pad - 12 * S, y + 38 * S)
                elif kind == "seg":
                    _, _, name, options, cur = row
                    L(pad, y + 14 * S, name, 13 if c else 14, "Semibold Text", 255, "lm")
                    self.segmented(key, options, cur, (pad, y + 26 * S, W - pad, y + 54 * S), 11)
                elif kind == "keybind":
                    _, _, name, binding, hint = row
                    for i, line in enumerate(names):
                        L(pad, y + (16 + i * 18) * S, line, 13 if c else 14, "Regular", 255, "lm")
                    text = "Press keys…" if self.hotkey_editing else binding
                    by = y + (20 + len(names)*18)*S
                    box = (pad, by, W-pad, by+30*S)
                    self.static(box, 15 * S, strength=5 * S, bevel=8 * S, zoom=.97,
                                rim=.8, frost=1.0, lift=.10, raised=.7)
                    self.hover_lens("hotkey:overlay", box, 15 * S)
                    L((box[0] + box[2]) / 2, (box[1] + box[3]) / 2,
                      self.fit(text, 11, "Semibold Text", box[2] - box[0] - 14 * S),
                      11, "Semibold Text", 255, "mm")
                    for i, line in enumerate(hints):
                        L(pad, by + (44 + i * 14) * S, line, 10, "Regular",
                          235 if self.hotkey_error else 205, "lm")
                else:
                    _, _, name, val, hint = row
                    for i, line in enumerate(names):
                        L(pad, y + (16 + i * 18) * S, line, 13 if c else 14, "Regular", 255, "lm")
                    for i, line in enumerate(hints):
                        L(pad, y + (24 + len(names) * 18 + i * 14) * S, line, 10, "Regular", 205, "lm")
                    self.toggle(key, val, W - pad - 48 * S, y + 4 * S)
            else:
                break
            y += rh
        if total > view_h:      # scroll hint
            bar_h = view_h * view_h / total
            by = top + (view_h - bar_h) * (off / max_off if max_off else 0)
            self.ink_shape((W - 9 * S, by, W - 6 * S, by + bar_h), 1.5 * S, 80)


class App:
    def __init__(self):
        self.mon = engine.Monitor()
        threading.Thread(target=self.mon.run, daemon=True).start()
        self.sound = Sound()
        self.media = NowPlaying()
        self.gaming = GamingMonitor()
        self.am = AppleMusic(refocus=lambda: user32.PostMessageW(self.hwnd, WM_APP_REFOCUS, 0, 0))
        self.hover_since = (None, 0.0)
        self.last_queue = 0.0
        self.queue_track = None
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
        self.glass = glass.GlassRenderer(S, round(Panel.GAMING_WIDE * S), round(self.panel.max_height() / self.ss))
        self.glass.set_supersample(self.ss)
        cfg = engine.load_config()
        if "BLOB_MVIEW" not in os.environ and cfg.get("music_view") in ("now", "bubble"):
            self.panel.music_view = cfg["music_view"]
        self.glassiness = float(cfg.get("glass", 0.35))
        self.captureable = bool(cfg.get("captureable", False))
        self.panel.set_compact(os.environ.get("BLOB_COMPACT", "1" if cfg.get("compact") else "0") == "1")
        self.panel.hardware_view = os.environ.get("BLOB_HARDWARE_VIEW", cfg.get("hardware_view", "card"))
        if self.panel.hardware_view not in ("card", "bubble"):
            self.panel.hardware_view = "card"
        self.panel.hardware_mode = os.environ.get("BLOB_HARDWARE_MODE", cfg.get("hardware_mode", "auto"))
        if self.panel.hardware_mode not in dict(HARDWARE_MODES):
            self.panel.hardware_mode = "auto"
        self.panel.update_width()
        self.panel.backdrop = bool(cfg.get("backdrop", False))
        self.panel.gaming_view = cfg.get("gaming_view", "strip")
        self.panel.options = dict(cfg.get("options", {}))
        self.hotkey_mods, self.hotkey_vk = parse_hotkey(cfg.get("overlay_hotkey", {}))
        self.panel.hotkey_label = hotkey_label(self.hotkey_mods, self.hotkey_vk)
        self.startup = startup_enabled()
        self.springs = Springs()
        self.springs.get("width", self.panel.w, k=185, zeta=0.86)
        self.springs.get("hardware_morph", 1.0 if self.panel.hardware_view == "bubble" else 0.0,
                         k=155, zeta=0.82)
        self.springs.get("music_morph", 1.0 if self.panel.music_view == "bubble" else 0.0,
                         k=155, zeta=0.82)
        self.springs.get("music_bass", 0.0, k=110, zeta=.68)
        self.springs.get("music_mid", 0.0, k=155, zeta=.72)
        self.springs.get("music_treble", 0.0, k=220, zeta=.74)
        self.music_peak_prev = 0.0
        # Bubble mode occupies the card's former top-right corner. The fixed
        # layered window has room below it, so expansion can grow back down.
        self.hardware_top = self.glass.panel_y(round((210 + Panel.TOP) * self.S))
        self.music_top = self.glass.panel_y(round((Panel.TOP + 118) * self.S))
        self.controls, self.old_controls = [], []
        self.visible, self.pinned = False, True
        self.audio_motion = AudioMotion()
        self._overlay_unlocked = True
        self.gaming_modifier_drag = False
        self.hotkey_editing = False
        self.hotkey_swallow = set()
        if PROFILE is not None:
            self.pinned = True  # profiling: keep the panel up even when focus moves elsewhere
        self.pos = None  # window top-left (screen px)
        self.drag = None
        self.drag_click = None
        self.drag_origin = None
        self.drag_moved = False
        self.music_press_active = False
        self.music_hold_fired = False
        self.music_click_pending = False
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
        self.cur_move = user32.LoadCursorW(None, 32646)
        self.apply_gaming_input()
        self.gaming_hotkey_registered = bool(user32.RegisterHotKey(
            self.hwnd, GAMING_HOTKEY, self.hotkey_mods | MOD_NOREPEAT, self.hotkey_vk))
        if not self.gaming_hotkey_registered:
            self.panel.hotkey_error = "Shortcut is already in use"
            engine.log(f"{self.panel.hotkey_label} is unavailable; use the tray menu to unlock the overlay.")

        self.icon = pystray.Icon(APP_NAME, tray_image(None), APP_NAME, menu=pystray.Menu(
            pystray.MenuItem("Open", lambda: user32.PostMessageW(self.hwnd, WM_APP_TOGGLE, 0, 0),
                             default=True, visible=False),
            pystray.MenuItem(lambda item: "Lock overlay" if self.overlay_unlocked else "Unlock overlay",
                             lambda: user32.PostMessageW(self.hwnd, WM_APP_GAMING_LOCK, 0, 0),
                             enabled=lambda item: self.visible and self.overlay_lock_available),
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
        self.gaming.set_active(self.visible and self.panel.page == "gaming")
        if self.panel.page in ("gaming", "settings"):
            self.panel.game = self.gaming.snapshot()
        self.panel.hw_note = self.hardware_note()
        control = (self.snap or {}).get("control", {})
        active = control.get("active")
        if active:
            reason = control.get("reason")
            self.panel.hardware_status = f"{str(active).title()} active" + (f" · {reason}" if reason else "")
        else:
            selected = dict(HARDWARE_MODES).get(self.panel.hardware_mode, "Auto")
            self.panel.hardware_status = f"{selected} selected · monitoring only"
        self.panel.hover_key = self.hover
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
        w = self.springs.get("width", self.panel.w, k=185, zeta=0.86)
        w.target = self.panel.w
        self.frame_dirty = True

    def hardware_note(self):
        src = (self.snap or {}).get("cpu_source")
        return {"hwmonitor": "LibreHardwareMonitor sensors, supplemented by HWiNFO when available. Fan RPM depends on the hardware and driver.",
                "hwinfo": "HWiNFO sensors. Enable Shared Memory Support; not every device exposes fan RPM.",
                "asus": "Read-only ASUS sensor fallback. Other PCs use LibreHardwareMonitor or HWiNFO.",
                "zone": "Windows ACPI provides a basic temperature. LibreHardwareMonitor adds accurate CPU, GPU and fan sensors.",
                "none": "Open LibreHardwareMonitor for CPU, GPU and fan temperatures on any supported PC."}.get(
                    src, "Hardware monitoring is read-only and vendor-neutral.")

    def setup_audio_support(self):
        """Run the same verified audio setup used by the full installer, then rescan devices."""
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "setup_system.ps1")
        if not os.path.exists(script):
            webbrowser.open("https://vb-audio.com/Cable/")
            return

        def install():
            try:
                subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                     "-InstallRoot", os.path.dirname(os.path.abspath(__file__)), "-SkipSensors"],
                    timeout=600)
            except Exception:
                engine.log("audio setup: " + __import__("traceback").format_exc())
            self.sound.refresh_devices()

        threading.Thread(target=install, daemon=True).start()

    def set_compact(self, compact):
        cfg = engine.load_config()
        cfg["compact"] = compact
        engine.save_config(cfg)
        self.panel.set_compact(compact)
        self.old_page_springs()
        self.springs.pop("height", None)

    def set_hardware_view(self, view):
        if view not in ("card", "bubble") or view == self.panel.hardware_view:
            return
        # Seed every dimension from the currently visible geometry before changing
        # the layout target. This turns the card/bubble switch into one continuous
        # spring motion instead of constructing the destination at full size.
        width = self.springs.get("width", self.panel.w, k=185, zeta=0.86)
        height = self.springs.get("height", self.panel.height(self.snap), k=260, zeta=0.82)
        morph = self.springs.get(
            "hardware_morph", 1.0 if self.panel.hardware_view == "bubble" else 0.0,
            k=155, zeta=0.82)
        # Keep the three geometric channels phase-aligned; otherwise the panel
        # briefly looks merely resized before its outline catches up.
        for spring in (width, height, morph):
            spring.k = 180.0
            spring.c = 2 * 0.82 * spring.k ** 0.5
        if self.panel.hardware_view == "card" and view == "bubble":
            self.hardware_top = self.glass.panel_y(round(height.x / self.ss))
        cfg = engine.load_config()
        cfg["hardware_view"] = view
        engine.save_config(cfg)
        self.panel.hardware_view = view
        self.panel.tabs_open = False
        self.panel.tabs_t = 0.0
        self.old_page_springs()
        self.panel.update_width()
        width.target = self.panel.w
        height.target = self.panel.height(self.snap)
        morph.target = 1.0 if view == "bubble" else 0.0

    def set_hardware_mode(self, mode):
        if mode not in dict(HARDWARE_MODES):
            return
        self.panel.hardware_mode = mode
        cfg = engine.load_config()
        cfg["hardware_mode"] = mode
        engine.save_config(cfg)

    def set_music_view(self, view):
        if view not in ("now", "art", "search", "queue", "bubble") or view == self.panel.music_view:
            return
        width = self.springs.get("width", self.panel.w, k=185, zeta=.86)
        height = self.springs.get("height", self.panel.height(self.snap), k=260, zeta=.82)
        morph = self.springs.get(
            "music_morph", 1.0 if self.panel.music_view == "bubble" else 0.0,
            k=155, zeta=.82)
        if view == "bubble":
            self.music_top = self.glass.panel_y(round(height.x / self.ss))
        if view in ("now", "bubble"):
            cfg = engine.load_config()
            cfg["music_view"] = view
            engine.save_config(cfg)
        self.panel.music_view = view
        self.panel.music_menu = None
        self.panel.tabs_open = False
        self.panel.tabs_t = 0.0
        self.springs.pop("tabs", None)
        self.panel.update_width()
        if view == "bubble" or morph.x > .001:
            for spring in (width, height, morph):
                spring.k = 180.0
                spring.c = 2 * .82 * spring.k ** .5
        width.target = self.panel.w
        height.target = self.panel.height(self.snap)
        morph.target = 1.0 if view == "bubble" else 0.0

    def start_music_bubble_press(self):
        self.music_press_active = True
        self.music_hold_fired = False
        user32.SetCapture(self.hwnd)
        user32.KillTimer(self.hwnd, 5)
        user32.SetTimer(self.hwnd, 5, 520, None)

    def finish_music_bubble_press(self):
        if not self.music_press_active:
            return False
        self.music_press_active = False
        user32.KillTimer(self.hwnd, 5)
        if user32.GetCapture() == self.hwnd:
            user32.ReleaseCapture()
        if self.music_hold_fired:
            self.music_hold_fired = False
            return True
        if self.music_click_pending:
            self.music_click_pending = False
            user32.KillTimer(self.hwnd, 4)
            self.media.next()
        else:
            self.music_click_pending = True
            user32.SetTimer(self.hwnd, 4, 280, None)
        return True

    def cycle_hardware_mode(self):
        cycle = ["auto", "quiet", "balanced", "performance"]
        try:
            index = cycle.index(self.panel.hardware_mode)
        except ValueError:
            index = -1
        self.set_hardware_mode(cycle[(index + 1) % len(cycle)])

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
        height = self.springs["height"].x / self.ss
        self.glass.capture(self.pos[0], self.pos[1], self.springs["width"].x / self.ss,
                           height, force=True, panel_y=self._panel_y(height))
        self.glass.source.frozen = True
        if was:
            user32.ShowWindow(self.hwnd, 4)   # SW_SHOWNOACTIVATE
        self.frame_dirty = True

    # ── visibility ──
    @property
    def gaming_locked(self):
        return self.panel.page == "gaming" and not self.overlay_unlocked

    @property
    def overlay_locked(self):
        return self.visible and not self.overlay_unlocked

    @property
    def gaming_drag_active(self):
        return self.gaming_locked and self.gaming_modifier_drag

    @property
    def bubble_mode(self):
        return ((self.panel.page == "blob" and self.panel.hardware_view == "bubble") or
                (self.panel.page == "music" and self.panel.music_view == "bubble") or
                (self.panel.page == "gaming" and getattr(self.panel, "gaming_view", "strip") == "bubble"))

    @property
    def bubble_drag_active(self):
        if not (self.visible and self.bubble_mode and self.overlay_unlocked):
            return False
        # Hardware's whole bubble is a click-or-drag surface.  Music keeps the
        # main lobe for playback gestures and uses only its satellite as the
        # click-or-drag handle.
        key = {"blob": "hview:card", "music": "mview:now", "gaming": "gview:strip"}.get(self.panel.page)
        return self.hover == key or self.drag_click == key

    def bubble_drag_key(self, key):
        if not (self.visible and self.bubble_mode and self.overlay_unlocked and key):
            return False
        return key == {"blob": "hview:card", "music": "mview:now", "gaming": "gview:strip"}.get(self.panel.page)

    @property
    def overlay_lock_available(self):
        return True

    @property
    def overlay_unlocked(self):
        return getattr(self, "_overlay_unlocked", True)

    # Compatibility aliases for older integrations; all views now share one state.
    @property
    def gaming_unlocked(self):
        return self.overlay_unlocked

    @gaming_unlocked.setter
    def gaming_unlocked(self, value):
        self._overlay_unlocked = bool(value)

    @property
    def bubble_unlocked(self):
        return self.overlay_unlocked

    @bubble_unlocked.setter
    def bubble_unlocked(self, value):
        self._overlay_unlocked = bool(value)

    def hotkey_modifiers_held(self):
        mods = getattr(self, "hotkey_mods", DEFAULT_HOTKEY[0])
        held = lambda vk: bool(user32.GetAsyncKeyState(vk) & 0x8000)
        checks = ((MOD_CONTROL, (0x11,)), (MOD_ALT, (0x12,)), (MOD_SHIFT, (0x10,)),
                  (MOD_WIN, (0x5B, 0x5C)))
        return bool(mods) and all(not (mods & flag) or any(held(vk) for vk in keys)
                                  for flag, keys in checks)

    def update_gaming_modifier_drag(self):
        """Temporarily accept drag input while the configured shortcut modifiers are held."""
        active = self.visible and self.gaming_locked and self.hotkey_modifiers_held()
        if active != self.gaming_modifier_drag:
            self.gaming_modifier_drag = active
            self.apply_gaming_input()

    def apply_gaming_input(self):
        # Layered + TRANSPARENT passes input to other processes, unlike merely
        # returning MA_NOACTIVATE or HTTRANSPARENT from a mouse message.
        style = user32.GetWindowLongPtrW(self.hwnd, -20)
        desired = style & ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        if self.panel.page == "gaming":
            desired |= WS_EX_NOACTIVATE
        if self.overlay_locked and not self.gaming_drag_active:
            desired |= WS_EX_TRANSPARENT
        if desired != style:
            user32.SetWindowLongPtrW(self.hwnd, -20, desired)
        if self.overlay_locked and not self.gaming_drag_active:
            self.drag = self.slider_drag = self.pressed = None
            self.drag_click = self.drag_origin = None
            self.drag_moved = False
            self.hover = self.mouse_xy = self.attached = self.detaching = None
            self.mouse_in = False
            self.panel.tabs_open = False
            if user32.GetCapture() == self.hwnd:
                user32.ReleaseCapture()
            user32.SetCursor(self.cur_arrow)

    def toggle_gaming_input(self):
        self.toggle_overlay_input()

    def toggle_overlay_input(self):
        if not self.visible:
            return
        self._overlay_unlocked = not self.overlay_unlocked
        self.gaming_modifier_drag = False
        # Expanded navigation makes edit mode obvious and provides a way out.
        if self.panel.page == "gaming":
            self.panel.tabs_open = self.overlay_unlocked
        self.apply_gaming_input()
        self.frame_dirty = True
        self.icon.update_menu()

    def begin_hotkey_edit(self):
        self.hotkey_editing = not self.hotkey_editing
        self.panel.hotkey_editing = self.hotkey_editing
        self.panel.hotkey_error = ""

    def set_overlay_hotkey(self, mods, vk):
        old = (self.hotkey_mods, self.hotkey_vk)
        if self.gaming_hotkey_registered:
            user32.UnregisterHotKey(self.hwnd, GAMING_HOTKEY)
        registered = bool(user32.RegisterHotKey(self.hwnd, GAMING_HOTKEY,
                                                mods | MOD_NOREPEAT, vk))
        if not registered:
            self.gaming_hotkey_registered = bool(user32.RegisterHotKey(
                self.hwnd, GAMING_HOTKEY, old[0] | MOD_NOREPEAT, old[1]))
            self.panel.hotkey_error = "That shortcut is already in use"
            return False
        self.gaming_hotkey_registered = True
        self.hotkey_mods, self.hotkey_vk = mods, vk
        self.panel.hotkey_label = hotkey_label(mods, vk)
        self.panel.hotkey_error = ""
        cfg = engine.load_config()
        cfg["overlay_hotkey"] = {"mods": mods, "vk": vk}
        engine.save_config(cfg)
        return True

    def maintain_gaming_topmost(self):
        if not self.visible or self.panel.page != "gaming":
            return
        # Games can themselves become topmost when focused. Creating Blob with
        # TOPMOST once does not keep it above another topmost window thereafter.
        foreground = user32.GetForegroundWindow()
        if not foreground or foreground == self.hwnd:
            return
        needs_raise = not (user32.GetWindowLongPtrW(self.hwnd, -20) & 0x8)
        if not needs_raise and user32.GetWindowLongPtrW(foreground, -20) & 0x8:
            above = user32.GetWindow(self.hwnd, 3)  # GW_HWNDPREV
            for _ in range(128):  # bounded: another process can reorder/destroy windows
                if not above:
                    break
                if above == foreground:
                    needs_raise = True
                    break
                above = user32.GetWindow(above, 3)
        if needs_raise:
            # NOMOVE | NOSIZE | NOACTIVATE | NOOWNERZORDER. Never change focus,
            # click-through, game settings, or the game's own window styles.
            user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x213)

    def show(self):
        self.snap = self.mon.snapshot() or self.snap or empty_snapshot()
        self.visible = True
        self.music_press_active = self.music_hold_fired = self.music_click_pending = False
        self.gaming_modifier_drag = False
        self.apply_gaming_input()
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
        user32.ShowWindow(self.hwnd, 4 if self.panel.page == "gaming" or self.overlay_locked else 5)
        if self.panel.page != "gaming" and self.overlay_unlocked:
            user32.SetForegroundWindow(self.hwnd)
        self.maintain_gaming_topmost()
        self._schedule()

    def hide(self):
        self.gaming_modifier_drag = False
        self.hotkey_editing = self.panel.hotkey_editing = False
        self.music_press_active = self.music_hold_fired = self.music_click_pending = False
        user32.KillTimer(self.hwnd, 4)
        user32.KillTimer(self.hwnd, 5)
        self.apply_gaming_input()
        self.gaming.set_active(False)
        self.mouse_in = False
        self.hover = None
        user32.KillTimer(self.hwnd, 1)
        self.interval = 0
        user32.ShowWindow(self.hwnd, 0)
        self.visible = False
        self.last_hide = time.time()

    def _schedule(self, animating=True):
        want = 16 if self.panel.page == "gaming" else 8
        if want != self.interval:
            self.interval = want
            user32.SetTimer(self.hwnd, 1, want, None)

    # ── lenses from controls + springs ──
    def resolve(self, controls, presence, prefix):
        S, sp = self.panel.S, self.springs  # all control geometry is supersampled until upload
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
                _, key, (tx0, ty0, tx1, ty1), segs, sel = c[:5]
                seg_alpha = c[5] if len(c) > 5 else 1.0
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
                self._fade(L, presence * seg_alpha)
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
        self.update_gaming_modifier_drag()
        dt = min(0.05, now - self.last_frame)
        self.last_frame = now
        self.springs.step(dt)
        fade = self.springs.get("fade", 1.0).x
        width = self.springs.get("width", self.panel.w, k=185, zeta=0.86)
        width.target = self.panel.w
        morph = self.springs.get(
            "hardware_morph", 1.0 if self.panel.hardware_view == "bubble" else 0.0,
            k=155, zeta=0.82)
        morph.target = 1.0 if ((self.panel.page == "blob" and self.panel.hardware_view == "bubble") or
                              (self.panel.page == "gaming" and self.panel.gaming_view == "bubble")) else 0.0
        music_morph = self.springs.get(
            "music_morph", 1.0 if self.panel.music_view == "bubble" else 0.0,
            k=155, zeta=.82)
        music_morph.target = 1.0 if self.panel.page == "music" and self.panel.music_view == "bubble" else 0.0
        bass = self.springs.get("music_bass", 0.0, k=110, zeta=.68)
        mid = self.springs.get("music_mid", 0.0, k=155, zeta=.72)
        treble = self.springs.get("music_treble", 0.0, k=220, zeta=.74)
        reactive = bool(self.panel.options.get("musicreactive", True))
        # Measure the actual output instead of trusting a player's transport
        # status. Browsers and several Spotify/YouTube integrations can keep a
        # valid media session while reporting a stale paused/opened state.
        reactive_live = bool(self.panel.page == "music" and self.panel.music_view in ("bubble", "art") and reactive)
        if hasattr(self.sound, "set_reactive_active"):
            self.sound.set_reactive_active(reactive_live)
        if reactive_live:
            self.sound.heartbeat()
            spectrum = np.asarray(getattr(self.sound, "spectrum", ()), np.float32)
            if spectrum.size >= 12 and float(spectrum.max(initial=0)) > .025:
                def band_level(values):
                    raw = .65 * float(np.mean(values)) + .35 * float(np.max(values))
                    return raw
                n = spectrum.size
                levels = (band_level(spectrum[:max(1, round(n * .32))]),
                          band_level(spectrum[round(n * .32):max(round(n * .32) + 1, round(n * .70))]),
                          band_level(spectrum[round(n * .70):]))
            else:
                # Peak-meter fallback is amplitude only, not invented frequency bands.
                peak = min(1.0, max(0.0, float(getattr(self.sound, "reactive_peak", 0.0))))
                levels = (peak, peak, peak)
            bass.target, mid.target, treble.target = self.audio_motion.update(levels, dt)
        else:
            bass.target, mid.target, treble.target = self.audio_motion.update((0, 0, 0), dt)
        tabs = self.springs.get("tabs", 0.0, k=300, zeta=0.85)
        tabs.target = 1.0 if self.panel.tabs_open else 0.0
        if abs(tabs.x - self.panel.tabs_t) > 0.004:     # redraw the labels as the pill opens
            self.panel.tabs_t = tabs.x
            self.draw_content()
        if self.panel.page == "music" and self.panel.music_view == "queue" and now - self.last_queue > 10:
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
            track = (m.title, m.artist)
            if m.active and track != self.queue_track:
                self.queue_track = track
                self.am.refresh_queue(force=True)
            seen = (m.art_version, m.playing, m.active, m.title)
            view = self.panel.music_view
            live = (view in ("now", "art", "bubble") and m.playing) or view == "search"  # progress / caret
            if seen != self.media_seen or self.am.version != self.am_seen or \
                    (live and now - self.last_text > (0.25 if view == "now" else 0.5)):
                if seen != self.media_seen:
                    self.media_seen = seen
                    self.glass.set_ambient(*album_tint(m.art))
                self.am_seen = self.am.version
                self.last_text = now
                self.draw_content()
        elif self.panel.page == "gaming" and now - self.last_text >= .5:
            self.last_text = now
            self.draw_content()
        viz_live = reactive_live or (self.panel.page == "sound" and
                    (self.sound.enabled or self.sound.spectrum.max() > 0.01))
        animating = self.springs.moving or self.drag is not None or bool(self.slider_drag)             or abs(self.vel).max() > 0.5
        force = animating or viz_live or self.frame_dirty
        # cheap path: poll the screen; draw only if the background or anything on the panel changed
        panel_h = self.springs["height"].x / self.ss
        panel_y = self._panel_y(panel_h)
        if not self.glass.capture(self.pos[0], self.pos[1], width.x / self.ss,
                                  panel_h, force, panel_y=panel_y):
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
            bands = self.sound.spectrum
            if self.panel.page == "music":
                bands = music_visualizer_bands(bands, getattr(self.sound, "reactive_peak", 0.0))
            self.glass.set_viz([v / self.ss for v in viz], bands, fade)
        else:
            self.glass.set_viz(None, None, 0)
        # light follows motion a little, like a droplet catching the light as it slides
        self.vel *= 0.85
        target = np.array([-0.55, -0.83]) + np.clip(self.vel * 0.02, -0.6, 0.6)
        self.light += (target - self.light) * 0.25
        self.light /= np.linalg.norm(self.light) + 1e-6
        self.light = np.round(self.light, 3)
        self.glass.set_panel_shape(max(morph.x, music_morph.x))
        if self.panel.page == "music":
            self.glass.set_bubble_audio(bass.x, mid.x, treble.x)
        else:
            self.glass.set_bubble_audio(0.0, 0.0, 0.0)
        self.glass.render(self.hwnd, self.pos[0], self.pos[1], width.x / self.ss,
                          panel_h, fade, tuple(self.light), self.glassiness, panel_y=panel_y)
        if fade >= 0.999 and self.old_controls:
            self.old_controls = []
            for k in [k for k in self.springs if k.startswith("old:")]:
                del self.springs[k]

    # ── glass pointer: fuses into what it hovers with surface tension, pulls, then snaps free ──
    SNAP = 15  # px the pointer can drag a button's glass before the bridge breaks

    def _update_pointer(self, lenses):
        S, sp = self.panel.S, self.springs
        style = "system" if self.bubble_mode else self.pointer_style
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
        S, sp = self.panel.S, self.springs
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

    def _panel_y(self, height):
        """Bubble morphs preserve the top edge of the card they came from."""
        morph = self.springs.get("hardware_morph", 0.0)
        if self.panel.page == "blob" and (self.panel.hardware_view == "bubble" or morph.x > .001):
            return self.hardware_top
        music_morph = self.springs.get("music_morph", 0.0)
        if self.panel.page == "music" and (self.panel.music_view == "bubble" or music_morph.x > .001):
            return self.music_top
        return self.glass.panel_y(int(round(height)))

    def panel_local(self, lp):
        """Mouse position in layout units (the panel is drawn supersampled)."""
        h = self.springs["height"].x / self.ss
        w = self.springs.get("width", self.panel.w).x / self.ss
        x = ctypes.c_short(lp & 0xFFFF).value - self.glass.panel_x(w)
        y = ctypes.c_short((lp >> 16) & 0xFFFF).value - self._panel_y(h)
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
                self.panel.music_menu = None
                self.apply_gaming_input()
                self.panel.update_width()
                if key == "gaming":
                    self.panel.tabs_open = False
                    self.panel.tabs_t = 0
                    self.springs.pop("tabs", None)
                crossfade = True
                if key == "sound":
                    self.sound.refresh_devices()
                self.startup = startup_enabled()
                self._schedule()
        elif kind == "details":
            self.panel.details_open = not self.panel.details_open
            crossfade = True
            self.old_page_springs()
        elif kind == "hmode":
            self.set_hardware_mode(key)
        elif kind == "hcycle":
            self.cycle_hardware_mode()
        elif kind == "hview":
            self.set_hardware_view(key)
            crossfade = True
        elif kind == "preset":
            self.sound.set_preset(key)
        elif kind == "mview":
            self.set_music_view(key)
            self.panel.scroll = {}
            crossfade = True
            self.old_page_springs()
            if key == "queue":
                self.am.refresh_queue()
                self.last_queue = time.perf_counter()
        elif kind == "musicutil":
            self.panel.music_menu = None if self.panel.music_menu == key else key
        elif kind == "result":
            self.am.play_result(int(key))
            self.hold_until = time.time() + 10
        elif kind == "queue":
            self.am.play_queue(int(key))
            self.hold_until = time.time() + 10
        elif kind == "size":
            self.set_compact(key == "compact")
            crossfade = True
        elif kind == "gview":
            if key in ("bubble", "strip"):
                self.panel.gaming_view = key
                self.panel.tabs_open = False
                self.panel.update_width()
                cfg = engine.load_config()
                cfg["gaming_view"] = key
                engine.save_config(cfg)
                crossfade = True
        elif kind == "searchbox":
            pass
        elif kind == "tabs":
            self.panel.tabs_open = not self.panel.tabs_open if self.panel.page == "gaming" else True
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
        elif kind == "hotkey" and key == "overlay":
            self.begin_hotkey_edit()
        elif kind == "toggle":
            if key == "sound":
                self.sound.set_enabled(not self.sound.enabled)
            elif key == "startup":
                set_startup(not startup_enabled())
                self.startup = startup_enabled()
            elif key == "capture":
                self.set_captureable(not self.captureable)
            elif key == "backdrop":
                self.panel.backdrop = not self.panel.backdrop
                cfg = engine.load_config()
                cfg["backdrop"] = self.panel.backdrop
                engine.save_config(cfg)
            elif key in ("queueart", "musicreactive", "soundstart"):
                opts = dict(self.panel.options)
                opts[key] = not opts.get(key, key != "soundstart")
                self.panel.options = opts
                cfg = engine.load_config()
                cfg["options"] = opts
                engine.save_config(cfg)
        elif kind == "device":
            self.sound.next_output()
        elif kind == "fxquit":
            self.sound.quit_fxsound()
        elif kind == "setupaudio":
            self.setup_audio_support()
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
            if msg == WM_APP_GAMING_LOCK or (msg == WM_HOTKEY and wp == GAMING_HOTKEY):
                self.toggle_overlay_input()
                return 0
            if self.gaming_drag_active:
                if msg == 0x84:  # WM_NCHITTEST: shortcut modifiers temporarily enable dragging
                    return 1  # HTCLIENT
                if msg == WM_LBUTTONDOWN:
                    cx, cy = cursor_pos()
                    self.drag = (cx - self.pos[0], cy - self.pos[1])
                    user32.SetCapture(hwnd)
                    return 0
                if msg == WM_MOUSEMOVE:
                    if self.drag:
                        x, y = cursor_pos()
                        nx, ny = x - self.drag[0], y - self.drag[1]
                        if abs(nx - self.pos[0]) + abs(ny - self.pos[1]) > 0:
                            self.vel += (nx - self.pos[0], ny - self.pos[1])
                            self.pos = [nx, ny]
                            self.pinned = True
                            if time.perf_counter() - self.last_frame >= 0.008:
                                self.frame()
                    return 0
                if msg in (WM_LBUTTONUP, WM_CAPTURECHANGED):
                    if self.drag:
                        self.drag = None
                        if user32.GetCapture() == hwnd:
                            user32.ReleaseCapture()
                        if self.captureable:
                            self.refresh_backdrop()
                    return 0
                if msg == WM_SETCURSOR:
                    user32.SetCursor(self.cur_move)
                    return 1
                if 0x200 <= msg <= 0x20E:
                    return 0
            elif self.overlay_locked:
                if msg == 0x84:  # WM_NCHITTEST; the layered style is the cross-process protection
                    return -1  # HTTRANSPARENT
                if 0x200 <= msg <= 0x20E or msg == WM_SETCURSOR:
                    return 0  # ignore any mouse messages queued before locking
            if msg == 0x21 and self.panel.page == "gaming":  # WM_MOUSEACTIVATE
                return 3  # MA_NOACTIVATE: dragging the strip must not steal the game's focus
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
            if msg == WM_TIMER and wp == 4:
                user32.KillTimer(hwnd, 4)
                if self.music_click_pending:
                    self.music_click_pending = False
                    self.media.toggle()
                return 0
            if msg == WM_TIMER and wp == 5:
                user32.KillTimer(hwnd, 5)
                if self.music_press_active:
                    self.music_hold_fired = True
                    self.media.previous()
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
                if self.visible and self.panel.page != "gaming":
                    user32.SetForegroundWindow(hwnd)
                return 0
            if msg == WM_ACTIVATE and (wp & 0xFFFF) == 0 and self.visible and not self.pinned \
                    and self.panel.page != "gaming" and not self.slider_drag and time.time() > self.hold_until:
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
                key = ("settings" if self.panel.page == "settings" else
                       "details" if self.panel.page == "blob" and self.panel.details_open else
                       view if self.panel.page == "music" and view in ("search", "queue") else None)
                if key:
                    step = -1 if ctypes.c_short(wp >> 16).value > 0 else 1
                    self.panel.scroll[key] = max(0, self.panel.scroll.get(key, 0) + step)
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
                    if self.drag_origin and not self.drag_moved:
                        self.drag_moved = abs(x - self.drag_origin[0]) + abs(y - self.drag_origin[1]) >= 5
                        if not self.drag_moved:
                            return 0
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
                    old_hover = self.hover
                    self.hover = self.panel.hit(x, y)
                    if old_hover != self.hover and self.panel.page == "music" and (
                            self.panel.music_view == "art" or
                            old_hover == "mview:art" or self.hover == "mview:art"):
                        self.draw_content()
                    want = (self.panel.tabs_open if self.panel.page == "gaming" else
                            bool(self.hover and (self.hover == "tabs" or self.hover.startswith("page:"))))
                    if want != self.panel.tabs_open:
                        self.panel.tabs_open = want
                    if time.perf_counter() - self.last_frame >= 0.006:
                        self.frame_dirty = True
                        self.frame()
                return 0
            if msg == 0x2A3 and not os.environ.get("BLOB_NOLEAVE"):  # WM_MOUSELEAVE (debug: ignore)
                had_music_hover = self.panel.page == "music" and bool(self.hover)
                self.mouse_in = False
                self.hover = None
                if self.panel.page != "gaming":
                    self.panel.tabs_open = False
                if had_music_hover:
                    self.draw_content()
                self.frame_dirty = True
                return 0
            if msg == WM_LBUTTONDOWN:
                x, y = self.panel_local(lp)
                h = self.panel.hit(x, y)
                if h == "mbubble:gesture":
                    self.start_music_bubble_press()
                elif self.bubble_drag_key(h):
                    cx, cy = cursor_pos()
                    self.drag = (cx - self.pos[0], cy - self.pos[1])
                    self.drag_click = h
                    self.drag_origin = (cx, cy)
                    self.drag_moved = False
                    user32.SetCapture(hwnd)
                elif h:
                    self.pressed = h
                    self.click(h, x)
                elif self.panel.page == "music" and self.panel.music_menu:
                    self.panel.music_menu = None
                    self.draw_content()
                    self.frame_dirty = True
                else:
                    cx, cy = cursor_pos()
                    self.drag = (cx - self.pos[0], cy - self.pos[1])
                    user32.SetCapture(hwnd)
                return 0
            if msg in (WM_LBUTTONUP, WM_CAPTURECHANGED):
                if self.music_press_active:
                    if msg == WM_LBUTTONUP:
                        self.finish_music_bubble_press()
                    else:
                        self.music_press_active = self.music_hold_fired = False
                        user32.KillTimer(hwnd, 5)
                    return 0
                deferred_click = self.drag_click if self.drag and not self.drag_moved and msg == WM_LBUTTONUP else None
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
                    self.drag_click = self.drag_origin = None
                    self.drag_moved = False
                    if user32.GetCapture() == hwnd:
                        user32.ReleaseCapture()
                    if self.captureable:
                        self.refresh_backdrop()
                if deferred_click:
                    self.click(deferred_click, 0)
                self._schedule(True)
                return 0
            if msg == WM_SETCURSOR:
                p = wintypes.POINT(*cursor_pos())
                user32.ScreenToClient(hwnd, ctypes.byref(p))
                lpv = (p.x & 0xFFFF) | ((p.y & 0xFFFF) << 16)
                x, y = self.panel_local(lpv)
                w, h = self.springs.get("width", self.panel.w).x, self.springs["height"].x
                inside = 0 <= x < w and 0 <= y < h
                glass_ptr = inside and self.pointer_style != "system" and not self.bubble_mode
                user32.SetCursor(None if glass_ptr else self.cur_move if self.bubble_drag_active else self.cur_arrow)
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
        self.maintain_gaming_topmost()  # 1 Hz, not part of rendering or capture
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
        fan_txt = f"\nFans {fans} rpm" if fans else ""
        self.icon.title = f"CPU {fmt_temp(t)}C · GPU {gpu_txt}{fan_txt}"[:127]
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
                    if k.vkCode in self.hotkey_swallow:
                        if not down:
                            self.hotkey_swallow.discard(k.vkCode)
                        return 1
                    if self.hotkey_editing:
                        if down:
                            self.hotkey_swallow.add(k.vkCode)
                            modifier_vks = {0x10, 0x11, 0x12, 0x5B, 0x5C}
                            if k.vkCode == 0x1B:
                                self.hotkey_editing = self.panel.hotkey_editing = False
                                self.panel.hotkey_error = "Shortcut edit cancelled"
                            elif k.vkCode not in modifier_vks:
                                held = lambda vk: bool(user32.GetAsyncKeyState(vk) & 0x8000)
                                mods = ((MOD_CONTROL if held(0x11) else 0) |
                                        (MOD_ALT if held(0x12) else 0) |
                                        (MOD_SHIFT if held(0x10) else 0) |
                                        (MOD_WIN if held(0x5B) or held(0x5C) else 0))
                                if not mods:
                                    self.panel.hotkey_error = "Include Ctrl, Alt, Shift, or Win"
                                elif self.set_overlay_hotkey(mods, k.vkCode):
                                    self.hotkey_editing = self.panel.hotkey_editing = False
                            self.draw_content()
                            self.frame_dirty = True
                        return 1
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
            user32.SetWindowDisplayAffinity(self.hwnd, 0 if self.captureable else 0x11)
            self.glass.source.frozen = self.captureable
            self.frame_dirty = True

    def quit(self):
        if self.gaming_hotkey_registered:
            user32.UnregisterHotKey(self.hwnd, GAMING_HOTKEY)
        self.gaming.shutdown()
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
    # Importing the layout for tests must not acquire the live app's single-instance lock.
    k32.CreateMutexW.restype = wintypes.HANDLE
    _mutex = k32.CreateMutexW(None, False, "Local\\BlobTrayApp-v3")
    if k32.GetLastError() == 183:
        sys.exit(0)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass
    try:
        App().run()
    except Exception:
        import traceback
        engine.log("fatal: " + traceback.format_exc())
        raise
