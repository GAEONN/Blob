"""Blob — tray app. The taskbar uses Blob's glass identity mark; click it for a liquid-glass
panel with hardware, sound, music, gaming and settings views. Hardware monitoring is read-only.
Sound provides system-wide boost / EQ like FxSound, with a glass spectrum. Drag the panel to pin it
anywhere (e.g. over a game or video); click the tray number again to hide it."""
import ctypes
import math
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
from PIL import Image, ImageDraw, ImageFont

import engine
import glass
from applemusic import AppleMusic
from media import NowPlaying
from gaming import GamingMonitor
from sound import PRESET_ORDER, Sound
from reactive import AudioMotion
from toolset import ClipboardController, ToolController
from glass import user32


def music_visualizer_bands(spectrum, peak):
    """Real DSP bins when available; otherwise an honest uniform amplitude meter."""
    bands = np.nan_to_num(np.asarray(spectrum, dtype=np.float32), nan=0, posinf=0, neginf=0)
    if bands.size == 28 and bands.max(initial=0) > .025:
        return np.clip(bands, 0, 1)
    peak = float(peak)
    peak = min(1.0, max(0.0, peak)) if np.isfinite(peak) else 0.0
    return np.full(28, np.sqrt(peak), dtype=np.float32)


APP_NAME = "Blob v5"
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
user32.CreateCursor.restype = wintypes.HANDLE
user32.CreateCursor.argtypes = [wintypes.HINSTANCE, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int,
                                ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_ubyte)]
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
if hasattr(user32, "GetDpiForWindow"):
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]

WM_DESTROY, WM_ACTIVATE, WM_SETCURSOR, WM_KEYDOWN, WM_TIMER = 0x02, 0x06, 0x20, 0x100, 0x113
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_CAPTURECHANGED = 0x200, 0x201, 0x202, 0x215
WM_SETTINGCHANGE, WM_DISPLAYCHANGE, WM_DPICHANGED = 0x1A, 0x7E, 0x02E0
WM_APP_TOGGLE, WM_APP_EXIT, WM_APP_REFOCUS = 0x8001, 0x8002, 0x8003
WM_APP_GAMING_LOCK, WM_HOTKEY, GAMING_HOTKEY = 0x8004, 0x312, 1
WS_EX_TRANSPARENT, WS_EX_NOACTIVATE = 0x20, 0x08000000
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
DEFAULT_HOTKEY = (MOD_CONTROL | MOD_ALT, ord("V"))
LEFT_CONTROL = 0xA2
RIGHT_CONTROL = 0xA3
DUAL_CONTROL_LABEL = "Left Ctrl + Right Ctrl"
BUBBLE_LINGER_SECONDS = 3.0
BUBBLE_PROXIMITY_K = 190.0
BUBBLE_PROXIMITY_ZETA = 0.92
GAMING_EDGE_DOCK_DIP = 30
DRAG_MONITOR_TRANSFER_DIP = 24
# Present at a stable 60 FPS.  Expensive Pillow texture updates are coalesced
# separately so they cannot starve the compositor while a menu spring moves.
FRAME_PACING_SECONDS = 1.0 / 60.0
TAB_TEXTURE_SECONDS = 1.0 / 30.0

# Bubble bodies are intentionally clean glass.  Their hit rectangles remain
# active, but the hover lens would add a second frosted circle over the SDF
# bubble and make the compact affordance look hazy.
BUBBLE_HOVER_KEYS = frozenset({
    "hcycle", "toggle:sound", "settingscycle", "mbubble:gesture",
    "gcycle", "hview:card", "sview:card", "settingsview:card", "mview:now", "gview:restore",
})

# MiniBlob is moved directly from its large body or its familiar return
# satellite. Tool-palette lobes deliberately stay out of this map: they always
# open their tool and never become accidental drag handles.
BUBBLE_DRAG_TARGETS = {
    "blob": frozenset(("hcycle", "hview:card")),
    "sound": frozenset(("toggle:sound", "sview:card")),
    "music": frozenset(("mbubble:gesture", "mview:now")),
    "gaming": frozenset(("gcycle", "gview:restore")),
    "settings": frozenset(("settingscycle", "settingsview:card")),
}


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


def album_palette(art, count=6):
    """Return a small stable palette for the GPU's moving colour field.

    The album image is reduced and quantized once per cover change. The shader
    then animates broad colour masses from these values, so the full backdrop
    stays filled without repeatedly blurring or rebuilding a large bitmap on
    the CPU.
    """
    fallback = (
        (0.12, 0.15, 0.20, 1.0), (0.18, 0.16, 0.22, 1.0),
        (0.12, 0.20, 0.24, 1.0), (0.22, 0.18, 0.14, 1.0),
        (0.16, 0.20, 0.28, 1.0), (0.20, 0.14, 0.20, 1.0),
    )
    if art is None:
        return fallback[:count]
    try:
        sample = art.convert("RGB").resize((36, 36), Image.LANCZOS)
        quantized = sample.quantize(colors=count, method=Image.Quantize.MEDIANCUT,
                                    dither=Image.Dither.NONE)
        raw = quantized.getpalette() or []
        colors = []
        entries = sorted(quantized.getcolors(maxcolors=256) or [], reverse=True)
        for _, index in entries:
            start = int(index) * 3
            if start + 2 >= len(raw):
                continue
            color = tuple(raw[start + channel] / 255.0 for channel in range(3))
            if any(sum((color[i] - prior[i]) ** 2 for i in range(3)) < 0.012
                   for prior in colors):
                continue
            colors.append(color)
            if len(colors) == count:
                break
        if not colors:
            mean = np.asarray(sample, np.float32).mean((0, 1)) / 255.0
            colors = [tuple(float(v) for v in mean)]
        while len(colors) < count:
            colors.append(colors[len(colors) % len(colors)])
        return tuple((*color, 1.0) for color in colors[:count])
    except Exception:
        return fallback[:count]


def S_of(panel):
    return panel.S


def rect_gap(x, y, rect):
    """Distance from a point to a rectangle (0 inside)."""
    dx = max(rect[0] - x, 0, x - rect[2])
    dy = max(rect[1] - y, 0, y - rect[3])
    return (dx * dx + dy * dy) ** 0.5


def work_rect_key(work):
    """A stable monitor-work-area identity without depending on ctypes equality."""
    return tuple(int(getattr(work, key)) for key in ("left", "top", "right", "bottom"))


def point_outside_work_area(x, y, work):
    """Distance outside a monitor work area (zero while the cursor is inside it)."""
    dx = max(float(work.left) - x, 0.0, x - float(work.right))
    dy = max(float(work.top) - y, 0.0, y - float(work.bottom))
    return math.hypot(dx, dy)


def gaming_dock_edge(panel_rect, work, threshold):
    """Return the deliberate Gaming drop edge, or None outside the edge gutter.

    The top wins an exact corner tie. That makes a deliberate pull into the
    top edge reliably produce the horizontal strip instead of a surprise
    vertical stack.
    """
    x, y, width, _ = panel_rect
    distances = (
        (abs(y - float(work.top)), 0, "top"),
        (abs(x - float(work.left)), 1, "left"),
        (abs(float(work.right) - (x + width)), 2, "right"),
    )
    distance, _, edge = min(distances)
    return edge if distance <= max(0.0, float(threshold)) else None


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
_TRAY_LOGO = None


def tray_image(temp):
    """Return Blob's glass identity mark, independent of the current temperature."""
    global _TRAY_LOGO
    if _TRAY_LOGO is None:
        try:
            logo = Image.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "blob-logo.png")).convert("RGBA")
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            logo.thumbnail((58, 58), resampling)
            canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            canvas.alpha_composite(logo, ((64 - logo.width) // 2, (64 - logo.height) // 2))
            _TRAY_LOGO = canvas
        except OSError:
            _TRAY_LOGO = None
    if _TRAY_LOGO is not None:
        return _TRAY_LOGO.copy()

    # Source-only fallback if the generated logo asset is missing.
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
GAMING_VIEWS = [("horizontal", "Horizontal"), ("vertical", "Vertical"), ("bubble", "Bubble")]
HARDWARE_MODES = [("auto", "Auto"), ("quiet", "Quiet"), ("balanced", "Balanced"),
                  ("performance", "Turbo"), ("custom", "Custom")]
POINTERS = [("arrow", "Arrow"), ("triangle", "Triangle"), ("droplet", "Droplet"), ("system", "System")]


def normalize_gaming_view(value):
    """Map the pre-v4 strip name without losing the supported Bubble state."""
    return {"strip": "horizontal"}.get(value, value) \
        if value in ("strip", "bubble", "horizontal", "vertical") else "horizontal"


class Panel:
    """Lays out one page: draws text into an ink layer and describes the glass controls.
    Glass controls are resolved into lenses every frame by App (so they can animate)."""

    TOP = 56    # room taken by the tab bar (which sits at the bottom)
    CONTENT = 18  # content starts this far below the top edge
    WIDE, NARROW = 340, 248
    BUBBLE_W, BUBBLE_H = 104, 98
    GAMING_WIDE, GAMING_NARROW = 624, 560
    GAMING_VERTICAL_WIDE, GAMING_VERTICAL_NARROW = 188, 172
    GAMING_VERTICAL_H = 424
    TOOL_W, TOOL_H = 420, 748
    MAGNIFIER_W, MAGNIFIER_H = 324, 324
    # Transparent canvas for concentric arc tools around the unchanged bubble.
    TOOL_PALETTE_W, TOOL_PALETTE_H = 220, 210
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
        self.tabs_side = "right"   # edge nearest the current monitor side
        self.anchor_side = "right" # the side the panel is parked on; mirrors bubbles and controls
        self.options = {}          # the small on/off settings, straight from the config file
        self.hw_note = ""          # what this machine can and cannot report
        self.game = {}
        self.w = round(self.WIDE * self.S)
        self.page = "blob"
        self.music_view = "now"
        self.music_menu = None       # contextual mini/cover utility: volume or options
        self.sound_menu = False      # physical output destinations
        self.hardware_view = "card"
        self.sound_view = "card"
        self.settings_view = "card"
        # SmallBlob offers two strip orientations plus the compact Bubble state.
        self.gaming_view = "horizontal"
        self.gaming_restore_view = "horizontal"
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
        # The tool layer is transient: it grows out of the active bubble and
        # returns to the same bubble when minimized.  The calculator model is
        # owned by App so every new card can keep its own cached session.
        self.tool_reveal = 0.0
        self.tools_open = False
        self.tool_view = None
        self.tool_minimized = False
        self.tool_glossiness = 0.78
        self.calculator_mode = "scientific"
        self.calculator_history = False
        self.calculator_history_page = 0
        self.calculator_notes_scroll = 0
        self.calculator_notes_cursor = None
        self.calculator_menu = None
        self.tool_size_scale = 1.0
        self._tool_fonts = None
        self.magnifier_zoom = 2.0

    def set_compact(self, compact):
        self.compact = compact
        self.update_width()

    def set_scale(self, scale):
        """Rebuild scale-dependent layout primitives after crossing a DPI boundary."""
        self.S = float(scale) * self.SS
        self.f = Fonts(self.S)
        self.update_width()

    def update_width(self):
        if self.tools_open:
            width = self.MAGNIFIER_W if self.tool_view == "magnifier" else self.TOOL_W
            if self.tool_view in ("calculator", "clipboard", "blank"):
                width *= self.tool_size_scale
        elif self.tool_reveal > .06 and self.tool_palette_available():
            width = self.TOOL_PALETTE_W
        elif ((self.page == "blob" and self.hardware_view == "bubble") or
                (self.page == "music" and self.music_view == "bubble") or
                (self.page == "sound" and self.sound_view == "bubble") or
                (self.page == "gaming" and self.gaming_view == "bubble") or
                (self.page == "settings" and self.settings_view == "bubble")):
            width = self.BUBBLE_W
        elif self.page == "music" and self.music_view == "art":
            # Compact must stay compact even when the album art view owns the
            # surface; otherwise the cover jumps back to the full-size page.
            width = self.NARROW if self.compact else self.WIDE
        else:
            if self.page == "gaming":
                widths = ((self.GAMING_VERTICAL_WIDE, self.GAMING_VERTICAL_NARROW)
                          if self.gaming_view == "vertical"
                          else (self.GAMING_WIDE, self.GAMING_NARROW))
            else:
                widths = (self.WIDE, self.NARROW)
            width = widths[bool(self.compact)]
        self.w = round(width * self.S)

    def height(self, s, page=None):
        page, c = page or self.page, self.compact
        if self.tools_open:
            if self.tool_view in ("calculator", "clipboard", "blank"):
                return round(self.tool_card_height() * self.S * self.tool_size_scale)
            return round((self.MAGNIFIER_H if self.tool_view == "magnifier" else self.TOOL_H) * self.S)
        if self.tool_reveal > .06 and self.tool_palette_available():
            return round(self.TOOL_PALETTE_H * self.S)
        if page == "blob" and self.hardware_view == "bubble":
            return round(self.BUBBLE_H * self.S)
        if page == "settings" and self.settings_view == "bubble":
            return round(self.BUBBLE_H * self.S)
        if page == "gaming":
            if self.gaming_view == "bubble":
                return round(self.BUBBLE_H * self.S)
            if self.gaming_view == "vertical":
                return round((self.GAMING_VERTICAL_H +
                              (56 if self.tabs_open or self.tabs_t > .04 else 0)) * self.S)
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
            if self.sound_view == "bubble":
                return round(self.BUBBLE_H * self.S)
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

    def tool_palette_available(self):
        """Only ordinary MiniBlob pages can grow the transient tool palette.

        Gaming is a fixed, game-safe FPS bubble.  It still uses the familiar
        hover restore satellite, but must never inherit the palette canvas or
        its curved tool lenses from another page.
        """
        if self.tools_open or self.page == "gaming":
            return False
        return ((self.page == "blob" and self.hardware_view == "bubble") or
                (self.page == "music" and self.music_view == "bubble") or
                (self.page == "sound" and self.sound_view == "bubble") or
                (self.page == "settings" and self.settings_view == "bubble"))

    @property
    def pad_u(self):
        if self.page == "music" and self.music_view == "art":
            return 4
        return 14 if self.compact else 22

    @property
    def radius(self):
        return self.RADIUS * self.S

    def gaming_menu_x(self, side=None):
        """Inset Gaming controls using the outer-radius/inner-radius rule.

        The panel corner is 34 DIP and the menu button is 15 DIP, leaving a
        19 DIP nested-corner inset instead of placing the button almost on the
        outer edge.
        """
        side = side or self.tabs_side
        return self.RADIUS * self.S if side == "left" else self.w - self.RADIUS * self.S

    def bubble_x(self, value):
        """Mirror a bubble-local x coordinate when the panel is parked left."""
        value = 104 - value if self.anchor_side == "left" else value
        offset = 0.0
        if self.tool_reveal > .06 and self.tool_palette_available() and self.anchor_side == "right":
            offset = (self.TOOL_PALETTE_W - 104) * self.S
        return offset + value * self.S

    def bubble_body_key(self):
        """Return the direct action owned by the primary MiniBlob lobe."""
        return {
            "blob": "hcycle",
            "sound": "toggle:sound",
            "music": "mbubble:gesture",
            "gaming": "gcycle",
            "settings": "settingscycle",
        }.get(self.page)

    def bubble_body_hit(self, x, y):
        """Recover the body hit while the transparent palette canvas is morphing.

        The rendered bubble is kept in place while the host canvas grows. During
        that short handoff the animated width can be between its old and new
        values, so a stale rectangle may miss the visible body. Use the actual
        lobe centre as a conservative fallback, but never claim the satellites
        or tool palette gaps.
        """
        key = self.bubble_body_key()
        if not key or self.tools_open or not self.bubble_main_box():
            return None
        cx, cy = self.bubble_x(43), 55 * self.S
        if math.hypot(x - cx, y - cy) <= 40 * self.S:
            return key
        return None

    def bubble_main_box(self):
        """Inset fused lobe; the monitor-facing satellite always owns the edge."""
        ox = 0.0
        if self.tool_reveal > .06 and self.tool_palette_available() and self.anchor_side == "right":
            ox = (self.TOOL_PALETTE_W - 104) * self.S
        if self.anchor_side == "left":
            return (22 * self.S + ox, 16 * self.S, 100 * self.S + ox, 94 * self.S)
        return (4 * self.S + ox, 16 * self.S, 82 * self.S + ox, 94 * self.S)

    def view_bubble_button(self, key, top, radius=15, glyph_size=12, y_offset=18):
        """One shared minimize/bubble anchor for every card page."""
        S, W = self.S, self.w
        pad = self.pad_u * S
        cx = pad + 16 * S if self.anchor_side == "left" else W - pad - 16 * S
        self.glass_button(key, cx, top + y_offset * S, radius * S, "\uE73F", glyph_size, always=True)

    def edge_safe_pad(self, pad):
        """Reserve the shared control rail when the panel is parked on the left."""
        return pad + (42 * self.S if self.anchor_side == "left" else 0)

    def inner_radius(self, inset, cap=None):
        """Nested corners: outer radius = inner radius + padding."""
        r = max(2 * self.S, self.radius - inset)
        return min(r, cap) if cap else r

    def calculator_height(self):
        if self.calculator_history:
            return 600
        return {"basic": 512, "notes": 600, "convert": 612}.get(self.calculator_mode, self.TOOL_H)

    def tool_card_height(self):
        """The calculator and Clipboard share one monitor-safe card contract."""
        return self.calculator_height() if self.tool_view == "calculator" else self.TOOL_H

    def tool_header_layout(self, leading=True):
        """Mirror a tool header toward the edge where its bubble will retract.

        Tool windows inherit the same spatial rule as every SmallBlob bubble:
        the minimize/close rail belongs to the monitor-facing side.  Keeping
        the opposite utility and title together makes the header readable on
        either monitor edge instead of simply flipping two isolated icons.
        """
        S, W = self.S, self.w
        left = self.anchor_side == "left"
        return dict(
            left=left,
            leading=(W - 40*S) if left else 40*S,
            title=(W - (70 if leading else 28)*S) if left else (70 if leading else 28)*S,
            title_anchor="rm" if left else "lm",
            new=124*S if left else W-124*S,
            minimize=86*S if left else W-86*S,
            close=48*S if left else W-48*S,
        )

    def max_height(self):
        return round(max(self.TOP + 300 + 16 * 24, self.TOOL_H + 40) * self.S)

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
                arc = getattr(self, "tool_arcs", {}).get(key)
                if arc:
                    cx, cy, orbit, thickness, angle, half_angle = arc
                    theta = math.atan2(y - cy, x - cx) - angle
                    theta = math.atan2(math.sin(theta), math.cos(theta))
                    nearest = angle + max(-half_angle, min(half_angle, theta))
                    # Match the shader's constant-radius arc capsule so every
                    # exposed end remains a round glass curve, never a fin.
                    radius = thickness
                    if math.hypot(x - cx - orbit * math.cos(nearest),
                                  y - cy - orbit * math.sin(nearest)) > radius:
                        continue
                return key
        return None

    def slider_value(self, key, x):
        x0, x1 = self.sliders[key]
        return (x - x0) / max(1, x1 - x0)

    # ── drawing helpers ──
    def _begin(self, H):
        self.pic = Image.new("RGBA", (self.w, H), (0, 0, 0, 0))
        self.backdrop_layer = None
        self.backdrop_palette = None
        self.ink = Image.new("L", (self.w, H), 0)
        self.accent = Image.new("RGBA", (self.w, H), (0, 0, 0, 0))
        self.di, self.da = ImageDraw.Draw(self.ink), ImageDraw.Draw(self.accent)
        self.controls, self.rects, self.sliders = [], {}, {}
        self.tool_arcs = {}

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
            text_style = "Semibold Text" if on else "Regular"
            text_size = 11 if icon else font_size
            # The menu is also used by the narrow vertical Gaming strip.  Do
            # not let a label paint outside its segment: first reduce it only
            # as much as needed, then ellipsize as a last resort.  This keeps
            # the main app's normal typography intact while preventing the
            # compact dropdown's labels from colliding or clipping.
            if not icon:
                available = max(1, sw - 8 * S)
                while text_size > 8 and self.text_font(name, text_size, text_style).getlength(name) > available:
                    text_size -= .5
                shown = name if self.text_font(name, text_size, text_style).getlength(name) <= available \
                    else self.fit(name, text_size, text_style, available)
            else:
                shown = name
            self.label(cx, ty0 + th / 2, shown, text_size, text_style,
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

    def _page_switcher(self, H, side=None, own_tabs_hit=True):
        """Draw the shared morphing page dropdown used by the full app.

        SmallBlob's Gaming strip has a permanent menu button, so it uses the
        same switcher animation without registering the resting bottom pill as
        a second ``tabs`` hit target.  ``side`` moves the resting pill to the
        nearest monitor edge; the expanded state still has the same readable
        full-width layout as the main app.
        """
        S, W = self.S, self.w
        t = max(0.0, min(1.0, self.tabs_t))
        pad = self.pad_u * S
        y0, y1 = H - 50 * S, H - 16 * S
        label = dict((k, n) for k, n in PAGES)[self.page]
        half_rest = (34 + 4.5 * len(label)) * S
        if side == "left":
            rest_x0, rest_x1 = pad, pad + 2 * half_rest
        elif side == "right":
            rest_x1, rest_x0 = W - pad, W - pad - 2 * half_rest
        else:
            rest_x0, rest_x1 = W / 2 - half_rest, W / 2 + half_rest
        lerp = lambda a, b: a + (b - a) * t
        box = (lerp(rest_x0, pad), lerp(y0 + 4 * S, y0),
               lerp(rest_x1, W - pad), lerp(y1 - 4 * S, y1))
        self.segmented("page", PAGES, self.page, box, 11 if self.compact else 13,
                       alpha=t, hit=t > 0.55)
        if t < 0.995:
            a = int(215 * (1 - t))
            rest_cx = (rest_x0 + rest_x1) / 2
            self.label(rest_cx - 7 * S, (box[1] + box[3]) / 2, label,
                       11 if self.compact else 12, "Semibold Text", a, "mm")
            self.di.text((rest_x0 + 2 * half_rest - 13 * S, (box[1] + box[3]) / 2), "\uE70E",
                         font=self.f.icon(8), fill=int(150 * (1 - t)), anchor="mm")
        if t < 0.55 and own_tabs_hit:
            self.rects["tabs"] = (box[0] - 6 * S, y0 - 6 * S, box[2] + 6 * S, y1 + 6 * S)
            self.controls.append(("hover", "tabs", box, (box[3] - box[1]) / 2))

    # ── pages ──
    def draw(self, s, snd, glassiness, startup, pointer="arrow", media=None, seek=None,
             captureable=False, tools=None, clipboard=None):
        if tools is not None:
            self.calculator_mode = tools.mode
        self.update_width()
        # the album backdrop goes down first, under everything else on the music page
        S, W = self.S, self.w
        H = self.height(s)
        self._begin(H)
        pad = self.pad_u * S
        if self.tools_open:
            saved_scale, saved_fonts = self.S, self.f
            try:
                if self.tool_view in ("calculator", "clipboard", "blank"):
                    self.S *= self.tool_size_scale
                    font_scale = max(self.S, self.SS)
                    if self._tool_fonts is None or self._tool_fonts.S != font_scale:
                        self._tool_fonts = Fonts(font_scale)
                    self.f = self._tool_fonts
                self._tool_card(tools, clipboard)
            finally:
                self.S, self.f = saved_scale, saved_fonts
            return self.ink, self.accent, self.controls
        if self.page == "blob" and self.hardware_view == "bubble":
            self._hardware_bubble(s)
            return self.ink, self.accent, self.controls
        if self.page == "sound" and self.sound_view == "bubble":
            self._sound_bubble(snd)
            return self.ink, self.accent, self.controls
        if self.page == "settings" and self.settings_view == "bubble":
            self._settings_bubble(glassiness)
            return self.ink, self.accent, self.controls
        if self.page == "gaming":
            self._gaming(s)
            return self.ink, self.accent, self.controls
        focus_art = self.page == "music" and self.music_view in ("art", "bubble")
        if not focus_art:
            # The switcher sits at the bottom of a bottom-anchored pane, so it
            # never moves between ordinary views. Expanded artwork owns the
            # whole surface and provides its own contextual collapse control.
            self._page_switcher(H)
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

    def _bubble_restore(self, key):
        """Use one visible, inset restore satellite for every page bubble."""
        # The return satellite belongs to the same hover reveal as the tool
        # palette. Keeping it in the idle layout was why it stayed visible
        # after the five-second linger expired.
        if self.tool_reveal <= .06 or self.tools_open:
            return
        S = self.S
        self.glass_button(key, self.bubble_x(82.5), 22.5 * S, 13 * S, "\uE72B", 10,
                          always=True, hover=False, frost=0.0)

    def _bubble_main_center(self, x=43):
        return self.bubble_x(x)

    def _hardware_bubble(self, s):
        """CPU/GPU temperatures at a glance; the satellite restores the System card."""
        S = self.S
        main = self.bubble_main_box()
        self.rects["hcycle"] = main
        self.controls.append(("hover", "hcycle", main, 39 * S))
        self._bubble_restore("hview:card")
        s = s or empty_snapshot()
        cpu, gpu = s.get("cpu", {}), s.get("gpu", {})
        self.label(self.bubble_x(27), 42 * S, "CPU", 8, "Semibold Text", 190, "mm")
        self.label(self.bubble_x(59), 42 * S, "GPU", 8, "Semibold Text", 190, "mm")
        self.label(self.bubble_x(27), 63 * S, fmt_temp(cpu.get("temp")), 18,
                   "Semibold Display", 255 if cpu.get("temp") is not None else 110,
                   "mm", temp=cpu.get("temp"))
        self.label(self.bubble_x(59), 63 * S, fmt_temp(gpu.get("temp")), 18,
                   "Semibold Display", 255 if gpu.get("temp") is not None else 110,
                   "mm", temp=gpu.get("temp"))
        self._draw_tool_satellites()

    def _sound_bubble(self, snd):
        """Compact audio status bubble with the same restore satellite as other pages."""
        S = self.S
        main = self.bubble_main_box()
        self.rects["toggle:sound"] = main
        self.controls.append(("hover", "toggle:sound", main, 39 * S))
        self._bubble_restore("sview:card")
        enabled = bool(getattr(snd, "enabled", False))
        boost = float(getattr(snd, "boost_db", 0) or 0)
        cx = self._bubble_main_center()
        self.label(cx, 42 * S, f"+{boost:.0f}", 21, "Semibold Display", 255, "mm")
        self.label(cx, 62 * S, "dB BOOST", 8, "Semibold Text", 190, "mm")
        self.label(cx, 81 * S, "ON" if enabled else "OFF", 8,
                   "Semibold Text", 220 if enabled else 155, "mm")
        self._draw_tool_satellites()

    def _settings_bubble(self, glassiness):
        """A quiet settings bubble so every top-level page has the same collapse flow."""
        S = self.S
        main = self.bubble_main_box()
        self.rects["settingscycle"] = main
        self.controls.append(("hover", "settingscycle", main, 39 * S))
        self._bubble_restore("settingsview:card")
        cx = self._bubble_main_center()
        self.label(cx, 42 * S, "SETTINGS", 8, "Semibold Text", 210, "mm")
        self.label(cx, 61 * S, f"{round(glassiness * 100)}%", 19,
                   "Semibold Display", 255, "mm")
        self.label(cx, 81 * S, "GLASS", 8, "Semibold Text", 175, "mm")
        self._draw_tool_satellites()

    def _gaming_metrics(self, s):
        """One metric model shared by both strip orientations."""
        game, cpu, gpu = self.game, s.get("cpu", {}), s.get("gpu", {})
        pct = lambda value: f"{value:.0f}% load" if value is not None else "No load sensor"
        fps, ms = game.get("fps"), game.get("frame_ms")
        fans = [fan.get("rpm") for fan in s.get("fans", [])]
        rpm = lambda index: f"{fans[index] / 1000:.1f}k" \
            if index < len(fans) and fans[index] is not None else "—"
        ram, ram_gb = game.get("ram_percent"), game.get("ram_gb")
        power = gpu.get("power")
        power_hint = "AC power" if s.get("power", {}).get("plugged") else "On battery"
        fps_hint = f"{ms:.1f} ms" if ms is not None else "Focus a game"
        status = game.get("status", "")
        if fps is None and "sign out" in status.lower():
            fps_hint = "Sign out once"
        elif fps is None and "Capture stopped" in status:
            fps_hint = "Retrying…"
        elif fps is None and any(word in status for word in
                                 ("permission", "installer", "Install", "unavailable", "Couldn't")):
            fps_hint = "Setup needed"
        return [
            ("FPS", f"{fps:.0f}" if fps is not None else "—", fps_hint, None),
            ("CPU", fmt_temp(cpu.get("temp")), pct(cpu.get("load")), cpu.get("temp")),
            ("GPU", fmt_temp(gpu.get("temp")), pct(gpu.get("load")), gpu.get("temp")),
            ("RAM", f"{ram:.0f}%" if ram is not None else "—",
             f"{ram_gb:.1f} GB" if ram_gb is not None else "System memory", None),
            ("FANS", rpm(0), f"{rpm(1)} rpm" if len(fans) > 1 else "RPM", None),
            ("GPU POWER", f"{power:.0f} W" if power is not None else "—", power_hint, None),
        ]

    def _gaming(self, s):
        """A single glass instrument strip, not a second dashboard over the game."""
        S, W, H = self.S, self.w, self.height(s)
        if self.gaming_view == "bubble":
            return self._gaming_bubble()
        if self.gaming_view == "vertical":
            return self._gaming_vertical(s)
        # Bubble contracts toward the monitor-facing edge; keep its control
        # there and put navigation on the inner edge so the two actions read
        # as separate, balanced anchors.
        bubble_side = self.anchor_side
        menu_side = "left" if bubble_side == "right" else "right"
        bubble_x = self.gaming_menu_x(bubble_side)
        menu_x = self.gaming_menu_x(menu_side)
        self.glass_button("gview:bubble", bubble_x, 43 * S, 15 * S, "\uE73F", 10, always=True)
        self.glass_button("tabs", menu_x, 43 * S, 15 * S, "\uE700", 12, always=True)
        metrics = self._gaming_metrics(s)
        # Reserve matching control rails on both ends: the Bubble affordance
        # sits opposite the menu, so neither control can collide with FPS or
        # GPU power when the strip changes monitor side.
        edge = max(self.pad_u, self.RADIUS - 8) * S
        rail = 40 * S
        left = edge + rail
        right = W - edge - rail
        step = (right - left) / len(metrics)
        for i, (name, value, hint, temp) in enumerate(metrics):
            x, width = left + i * step, step - 10 * S
            self.label(x, 17 * S, name, 10, "Semibold Text", 170)
            self.label(x, 30 * S, self.fit(value, 23, "Semibold Display", width), 23,
                       "Semibold Display", 255, temp=temp)
            self.label(x, 61 * S, self.fit(hint, 11, "Regular", width), 11, "Regular", 190)
        # The navigation stays tucked away until the menu is opened.
        if self.tabs_open or self.tabs_t > .04:
            self._page_switcher(H, menu_side, own_tabs_hit=False)

    def _gaming_vertical(self, s):
        """The same six-metric strip language stacked vertically."""
        S, W, H = self.S, self.w, self.height(s)
        bubble_side = self.anchor_side
        menu_side = "left" if bubble_side == "right" else "right"
        bubble_x = self.gaming_menu_x(bubble_side)
        menu_x = self.gaming_menu_x(menu_side)
        self.glass_button("gview:bubble", bubble_x, 31 * S, 15 * S, "\uE73F", 10, always=True)
        self.glass_button("tabs", menu_x, 31 * S, 15 * S, "\uE700", 12, always=True)
        metrics = self._gaming_metrics(s)
        top = 54 * S
        row = 57 * S
        max_width = W - 34 * S
        for i, (name, value, hint, temp) in enumerate(metrics):
            y = top + i * row
            self.label(17 * S, y, name, 10, "Semibold Text", 170, "la")
            self.label(W - 17 * S, y, self.fit(value, 23, "Semibold Display", max_width),
                       23, "Semibold Display", 255, "ra", temp=temp)
            self.label(17 * S, y + 28 * S, self.fit(hint, 11, "Regular", max_width),
                       11, "Regular", 190, "la")
        if self.tabs_open or self.tabs_t > .04:
            self._page_switcher(H, menu_side, own_tabs_hit=False)

    def _gaming_bubble(self):
        """Small FPS/frame-time bubble with an inset restore satellite."""
        S = self.S
        main = self.bubble_main_box()
        # Match every other MiniBlob: the useful primary face is a direct
        # drag surface, while a short release keeps its normal tap behavior.
        self.rects["gcycle"] = main
        self.controls.append(("hover", "gcycle", main, 39 * S))
        fps, ms = self.game.get("fps"), self.game.get("frame_ms")
        cx = self._bubble_main_center()
        self.label(cx, 43*S, f"{fps:.0f}" if fps is not None else "—", 24,
                   "Semibold Display", 255, "mm")
        self.label(cx, 63*S, "FPS", 10, "Semibold Text", 195, "mm")
        self.label(cx, 80*S, f"{ms:.1f} ms" if ms is not None else "— ms", 11,
                   "Regular", 235, "mm")
        # The satellite restores the strip; the main body and satellite are
        # both direct drag surfaces, matching every other MiniBlob view.
        self._bubble_restore("gview:restore")
        self._draw_tool_satellites()

    def fit(self, text, size, style, max_w):
        """Ellipsize text to max_w pixels.

        Clipboard rows can hold 100k characters, so never measure more than
        could possibly fit, and binary-search the cut instead of trimming one
        character per measurement.
        """
        text = str(text)
        font = self.text_font(text[:512], size, style)
        # No glyph is narrower than a fifth of the em, so anything beyond this
        # many characters is already off the end of the line.
        cap = int(max_w / max(1.0, .2 * getattr(font, "size", size))) + 2
        clipped = len(text) > cap
        text = text[:cap]
        if not clipped and font.getlength(text) <= max_w:
            return text
        if font.getlength("…") > max_w:
            return ""
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.getlength(text[:mid] + "…") <= max_w:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo].rstrip() + "…"

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

    def glass_button(self, key, cx, cy, r, glyph, size, always=False, hover=True, frost=1.0):
        """Round glass button with optional idle body and hover lens."""
        S = self.S
        rect = (cx - r, cy - r, cx + r, cy + r)
        if always:
            self.static(rect, r, strength=7 * S, bevel=r * 0.8, zoom=0.9, rim=0.9, frost=frost, lift=0.14,
                        raised=1.0)
        self.rects[key] = rect
        if hover:
            self.controls.append(("hover", key, rect, r))
        self.di.text((cx, cy), glyph, font=self.f.icon(size), fill=255, anchor="mm")

    def tool_button(self, key, cx, cy, r, text=None, tint=None, icon=None, size=11,
                    width=None, height=None):
        """Frosted tool control using the shared Fluent glyph family."""
        S = self.S
        width = width if width is not None else 2 * r
        height = height if height is not None else 2 * r
        rect = (cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)
        radius = min(r, height / 2, width / 2)
        kwargs = dict(strength=9 * S, bevel=r * .82, zoom=.92, rim=.85,
                      frost=self.tool_glossiness, lift=.13, raised=1.0)
        if tint is not None:
            kwargs["tint"] = tint
        self.static(rect, r, **kwargs)
        self.rects[key] = rect
        self.controls.append(("hover", key, rect, radius))
        if icon:
            self.di.text((cx, cy), icon, font=self.f.icon(size), fill=245, anchor="mm")
        elif text:
            self.label(cx, cy, text, size, "Semibold Text", 245, "mm")

    def _draw_tool_satellites(self):
        """Four concentric curved glass segments, following the user's sketch."""
        reveal = max(0.0, min(1.0, self.tool_reveal))
        if reveal <= .06 or not self.tool_palette_available():
            return
        S = self.S
        cx, cy = self.bubble_x(43), 55 * S
        # Every named tool uses the same Fluent vector family. The fourth
        # satellite intentionally stays blank: it is a quiet, editable slot
        # for the next tool rather than a misleading currency shortcut.
        items = (("calculator", "\uE8EF", 16), ("clipboard", "\uE8C8", 13),
                 ("magnifier", "\uE721", 13), ("blank", None, 0))
        for index, (tool, icon, icon_size) in enumerate(items):
            key = "tool:restore" if self.tool_minimized and self.tool_view == tool else "tool:" + tool
            delay = index * .045
            phase = max(0.0, min(1.0, (reveal - .06 - delay) / (.94 - delay)))
            if phase <= .01:
                continue
            ease = phase * phase * (3.0 - 2.0 * phase)
            # A slightly wider fan leaves a real breathable gap between the
            # now-round caps once the palette has finished extruding.
            angle = math.radians(204 - index * 46 + 8 * (1 - ease))
            if self.anchor_side == "left":
                angle = math.pi - angle
            # Start inside the body, then stretch out through a liquid neck.
            # Substantial round caps keep the finished pieces plump, not rails.
            orbit = (22 + 46 * ease) * S
            thickness = (14 + 3 * ease) * S
            # Longer curved lobes read as connected blobs instead of fat dots.
            # Keep a real transparent gap between the thicker pieces.
            half_angle = math.radians(3 + 5 * ease)
            outer = orbit + thickness
            bounds = (cx - outer, cy - outer, cx + outer, cy + outer)
            self.controls.append(("arc", key, dict(
                rect=bounds, r=thickness, arc=(angle, half_angle),
                strength=4 * S * ease, bevel=14 * S, zoom=1.0,
                rim=.42 * ease, frost=.18 * ease, lift=.04 * ease,
                raised=.35 * ease)))
            # Hit testing uses the same circular centreline as the shader;
            # the gaps between segments remain transparent to interaction.
            self.tool_arcs[key] = (cx, cy, orbit, thickness, angle, half_angle)
            angles = [angle - half_angle, angle + half_angle, angle]
            for cardinal in (0, math.pi / 2, math.pi, -math.pi / 2):
                delta = math.atan2(math.sin(cardinal - angle), math.cos(cardinal - angle))
                if abs(delta) <= half_angle:
                    angles.append(cardinal)
            xs = [cx + orbit * math.cos(a) for a in angles]
            ys = [cy + orbit * math.sin(a) for a in angles]
            self.rects[key] = (min(xs) - thickness, min(ys) - thickness,
                               max(xs) + thickness, max(ys) + thickness)
            x, y = cx + orbit * math.cos(angle), cy + orbit * math.sin(angle)
            if ease < .65:
                self.rects.pop(key, None)  # emerging glass must not steal body clicks
            ink_phase = max(0.0, min(1.0, (ease - .5) / .5))
            alpha = round(245 * ink_phase * ink_phase * (3 - 2 * ink_phase))
            if icon:
                self.di.text((x, y), icon, font=self.f.icon(icon_size), fill=alpha, anchor="mm")

    def _tool_card(self, tools, clipboard=None):
        """Reference-inspired keys and generous display, in Blob's own glass."""
        S, W = self.S, self.w
        if self.tool_view == "magnifier":
            self._magnifier_card()
            return
        if self.tool_view == "clipboard":
            self._clipboard_card(clipboard)
            return
        if self.tool_view == "blank":
            self._blank_tool_card()
            return
        if tools is None:
            return
        calc, mode = tools.calculator, tools.mode
        header = self.tool_header_layout()
        self.glass_button("calcpanel:history", header["leading"], 36*S, 17*S, "\uE81C", 16, always=True)
        self.label(header["title"], 36*S, "Calculator", 16, "Semibold Text", 245, header["title_anchor"])
        self.glass_button("tool:new", header["new"], 36*S, 15*S, "\uE710", 12, always=True)
        self.glass_button("tool:minimize", header["minimize"], 36*S, 15*S, "\uE73F", 12, always=True)
        self.glass_button("tool:close", header["close"], 36*S, 15*S, "\uE711", 12, always=True)
        titles = {"basic": "Basic", "scientific": "Scientific", "notes": "Math Notes", "convert": "Convert"}
        if self.calculator_history:
            self._calculator_history(tools)
            return
        mode_label = ("‹  " + titles[mode]) if header["left"] else (titles[mode] + "  ›")
        self.tool_button("calcmenu:mode", 82*S if header["left"] else W-82*S, 88*S, 17*S,
                         text=mode_label, width=124*S, size=13)
        if mode == "notes":
            self._calculator_notes(tools)
        else:
            self.label(24*S, 88*S, calc.angle_mode.title() if mode != "convert" else "Convert",
                       12, "Regular", 200, "lm")
            expression = calc.expression or "0"
            shown = calc.result if calc.last_was_eval else expression
            if mode == "convert":
                shown = expression
            # Fit the entire result by reducing only unusually long numbers,
            # never ellipsize a result into a misleading different value.
            try:
                if calc.last_was_eval and "e" not in shown.lower():
                    shown = format(float(shown), ",.12g")
            except ValueError:
                pass
            self.label(W-24*S, 121*S, self.fit(expression if calc.last_was_eval else "", 13,
                       "Regular", W-48*S), 13, "Regular", 185, "rm")
            size = 44
            while size > 18 and self.f.get(size, "Semibold Display").getlength(shown) > W-48*S:
                size -= 1
            self.label(W-24*S, 164*S, self.fit(shown, size, "Semibold Display", W-48*S),
                       size, "Semibold Display", 255, "rm")
            if mode == "convert":
                self._calculator_conversion(tools)
            else:
                if mode == "scientific":
                    scientific = (("(", ")", "mc", "m+", "m−", "mr"),
                                  ("2nd", "x²", "x³", "xʸ", "eˣ", "10ˣ"),
                                  ("1/x", "²√x", "³√x", "ʸ√x", "ln", "log₁₀"),
                                  ("x!", "sin", "cos", "tan", "e", "EE"),
                                  ("Rand", "sinh", "cosh", "tanh", "π", "Deg"))
                    inverse = {"sin": ("asin", "sin⁻¹"), "cos": ("acos", "cos⁻¹"),
                               "tan": ("atan", "tan⁻¹"), "sinh": ("asinh", "sinh⁻¹"),
                               "cosh": ("acosh", "cosh⁻¹"), "tanh": ("atanh", "tanh⁻¹")}
                    gap, width = 7*S, (W-48*S-35*S)/6
                    for row, keys in enumerate(scientific):
                        for col, token in enumerate(keys):
                            shown_key = token
                            if token == "log₁₀":
                                token = "log10"
                            if calc.second and token in inverse:
                                token, shown_key = inverse[token]
                            if shown_key == "Deg":
                                shown_key = "Rad" if calc.angle_mode == "DEG" else "Deg"
                            self.tool_button("calc:"+token, 24*S+col*(width+gap)+width/2,
                                             (223+row*45)*S, 19*S, text=shown_key,
                                             size=12, width=width, height=38*S,
                                             tint=(1, 1, 1, .18) if shown_key == "2nd" and calc.second else None)
                self._calculator_keypad(438 if mode == "scientific" else 202)
                self.label(24*S, (730 if mode == "scientific" else 494)*S,
                           "M" if calc.memory else "Saved on this device", 10, "Regular", 175, "lm")
        if self.calculator_menu:
            self._calculator_menu(tools)

    def _calculator_keypad(self, top, height=48, gap=8):
        S, W = self.S, self.w
        keys = (("DEL", "AC", "%", "÷"), ("7", "8", "9", "×"),
                ("4", "5", "6", "−"), ("1", "2", "3", "+"), ("±", "0", ".", "="))
        width = (W-48*S-3*gap*S)/4
        accents = {"+": (.2, .8, .4, .38), "−": (.94, .28, .24, .36),
                   "=": (.96, .65, .18, .55), "×": (.96, .65, .18, .30), "÷": (.96, .65, .18, .30)}
        for row, line in enumerate(keys):
            for col, key in enumerate(line):
                self.tool_button("calc:"+key, 24*S+col*(width+gap*S)+width/2,
                                 (top+row*(height+gap)+height/2)*S, height*S/2,
                                 text=None if key == "DEL" else key,
                                 icon="\uE750" if key == "DEL" else None,
                                 tint=accents.get(key), size=23 if key not in ("AC", "DEL") else 18,
                                 width=width, height=height*S)

    def _calculator_history(self, tools):
        S, W = self.S, self.w
        rows = tools.recent(len(tools.history))
        pages = max(1, math.ceil(len(rows)/6))
        self.calculator_history_page = min(max(0, self.calculator_history_page), pages-1)
        offset = self.calculator_history_page*6
        self.label(24*S, 84*S, "History", 23, "Semibold Display", 245, "lm")
        self.label(W-24*S, 84*S, f"{len(rows)} saved", 12, "Regular", 190, "rm")
        if not rows:
            self.label(W/2, 240*S, "Your calculations will appear here.", 15, "Regular", 220, "mm")
        for index, entry in enumerate(rows[offset:offset+6]):
            y = (112+index*70)*S
            rect = (24*S, y, W-24*S, y+60*S)
            self.static(rect, 18*S, frost=self.tool_glossiness, lift=.12, strength=4*S, bevel=12*S, rim=.4)
            self.rects["calcrecall:"+str(offset+index)] = rect
            self.label(38*S, y+18*S, self.fit(str(entry.get("expression", "")), 13, "Regular", W-76*S),
                       13, "Regular", 195, "lm")
            self.label(W-38*S, y+43*S, self.fit(str(entry.get("result", "")), 21, "Semibold Text", W-76*S),
                       21, "Semibold Text", 255, "rm")
        self.tool_button("calchistory:prev", 44*S, 564*S, 17*S, icon="\uE76B", size=12)
        self.label(W/2, 564*S, f"{self.calculator_history_page+1} / {pages} · Click to reuse",
                   12, "Regular", 210, "mm")
        self.tool_button("calchistory:next", W-44*S, 564*S, 17*S, icon="\uE76C", size=12)

    def _calculator_notes(self, tools):
        S, W = self.S, self.w
        self.label(24*S, 88*S, "Math Notes", 17, "Semibold Text", 240, "lm")
        self.label(24*S, 126*S, "Type expressions or variables, one per line.", 12, "Regular", 195, "lm")
        lines, results = tools.notes.split("\n"), tools.note_results()
        visible = 10
        start = max(0, min(self.calculator_notes_scroll, max(0, len(lines)-visible)))
        box = (24*S, 150*S, W-24*S, 550*S)
        self.static(box, 20*S, frost=max(.6, self.tool_glossiness), lift=.14, strength=3*S, bevel=12*S, rim=.4)
        self.rects["calcpanel:notes"] = box
        for index, line in enumerate(lines[start:start+visible]):
            y = (174+index*37)*S
            self.label(38*S, y, self.fit(line or ("Type here…" if not tools.notes else ""), 14,
                       "Regular", W*.58-42*S), 14, "Regular", 245, "lm")
            result = results[start+index] if start+index < len(results) else ""
            self.label(W-38*S, y, self.fit(result, 15, "Semibold Text", W*.32), 15,
                       "Semibold Text", 245, "rm")
            self.rects["calcnotecursor:"+str(start+index)] = (box[0], y-17*S, W*.63, y+17*S)
        cursor = len(tools.notes) if self.calculator_notes_cursor is None else min(self.calculator_notes_cursor, len(tools.notes))
        before = tools.notes[:cursor]
        row = before.count("\n") - start
        if 0 <= row < visible:
            prefix = before.rsplit("\n", 1)[-1]
            x = min(W*.58-4*S, 38*S+self.f.get(14).getlength(prefix))
            y = (174+row*37)*S
            self.di.line((x, y-8*S, x, y+8*S), fill=230, width=max(1, round(S)))
        self.label(24*S, 574*S, "Example: price=120   then   price*1.16", 12, "Regular", 195, "lm")

    def _calculator_conversion(self, tools):
        S, W = self.S, self.w
        self.tool_button("calcmenu:kind", 91*S, 205*S, 16*S, text=tools.convert_kind+" ›",
                         width=134*S, size=13)
        self.tool_button("calcmenu:from", W-150*S, 205*S, 16*S, text=tools.convert_from+" ›",
                         width=86*S, size=13)
        self.tool_button("calcmenu:to", W-55*S, 205*S, 16*S, text=tools.convert_to+" ›",
                         width=70*S, size=13)
        result = tools.conversion()
        self.label(24*S, 257*S, "=", 24, "Regular", 215, "lm")
        self.label(W-24*S, 257*S, self.fit(result+" "+tools.convert_to, 28, "Semibold Display", W-85*S),
                   28, "Semibold Display", 255, "rm")
        self._calculator_keypad(304, height=46, gap=7)
        note = (tools.rate_status or f"Frankfurter · {tools.rate_date or 'No cached rates'}") if tools.convert_kind == "Currency" else "Unit conversion · saved with calculator"
        self.label(24*S, 588*S, self.fit(note, 11, "Regular", W-96*S), 11, "Regular", 190, "lm")
        if tools.convert_kind == "Currency":
            self.glass_button("calcrate:refresh", W-38*S, 588*S, 13*S, "\uE72C", 11, always=True)

    def _calculator_menu(self, tools):
        S, W = self.S, self.w
        menu = self.calculator_menu
        if menu == "mode":
            items = [("basic", "Basic"), ("scientific", "Scientific"), ("notes", "Math Notes"), ("convert", "Convert")]
            selected = tools.mode
        elif menu == "kind":
            items = [(key, key) for key in tools.UNITS]
            selected = tools.convert_kind
        else:
            items = [(key, key) for key in tools.UNITS[tools.convert_kind]]
            selected = getattr(tools, "convert_"+menu)
        box = ((20*S, 66*S, 256*S, (82+len(items)*40)*S)
               if self.anchor_side == "left"
               else (W-256*S, 66*S, W-20*S, (82+len(items)*40)*S))
        self.di.rectangle(box, fill=0)
        # Opaque ink and active targets behind the popover must not leak through.
        self.rects = {key: value for key, value in self.rects.items() if value[3] < 66*S}
        self.rects["calcmenu:close"] = (0, 66*S, W, self.ink.height)
        self.controls = [c for c in self.controls if c[0] != "static" or
                         c[1]["rect"][3] <= box[1] or c[1]["rect"][1] >= box[3] or
                         c[1]["rect"][2] <= box[0]]
        self.static(box, 24*S, frost=1, lift=.22, strength=5*S, bevel=18*S, rim=.6)
        for index, (value, label) in enumerate(items):
            y = (74+index*40)*S
            key = ("toolmode:" if menu == "mode" else "calcunit:"+menu+":") + value
            self.rects[key] = (box[0]+8*S, y, box[2]-8*S, y+40*S)
            if selected == value:
                self.di.text((box[0]+22*S, y+20*S), "\uE73E", font=self.f.icon(12), fill=245, anchor="mm")
            self.label(box[0]+44*S, y+20*S, label, 15, "Semibold Text" if selected == value else "Regular", 245, "lm")

    def _clip_thumb(self, clipboard, item, box, radius, contain=False):
        """Composite a cached picture preview into ``box``; False when there is none."""
        thumb = clipboard.thumbnail(item) if clipboard and item else None
        if thumb is None:
            return False
        x0, y0, x1, y1 = [int(round(v)) for v in box]
        w, h = max(1, x1 - x0), max(1, y1 - y0)
        cache = self.__dict__.setdefault("_clip_thumb_cache", {})
        key = (item["id"], w, h, contain)
        img = cache.get(key)
        if img is None:
            iw, ih = thumb.size
            if contain:
                scale = min(w / iw, h / ih)
                fw, fh = max(1, round(iw * scale)), max(1, round(ih * scale))
                img = thumb.resize((fw, fh), Image.LANCZOS)
            else:
                # Cover-crop, like album art: fill the tile without squashing.
                scale = max(w / iw, h / ih)
                img = thumb.resize((max(w, round(iw * scale)), max(h, round(ih * scale))), Image.LANCZOS)
                l, t = (img.width - w) // 2, (img.height - h) // 2
                img = img.crop((l, t, l + w, t + h))
            mask = (glass.shape_mask(img.width, img.height, min(radius, img.width / 2, img.height / 2)) * 255)
            alpha = np.minimum(np.asarray(img.getchannel("A"), dtype=np.float32), mask).astype(np.uint8)
            img.putalpha(Image.fromarray(alpha))
            if len(cache) > 96:
                cache.clear()
            cache[key] = img
        self.pic.alpha_composite(img, (x0 + (w - img.width) // 2, y0 + (h - img.height) // 2))
        return True

    def _clipboard_mark(self, cx, cy, kind, alpha=225, size=18):
        """Compact vector marks for clipboard kinds; no generic emoji glyphs."""
        S, d = self.S, self.di
        u, stroke = size * S, max(1, round(1.4 * S))
        if kind == "Files":
            box = (cx-u*.62, cy-u*.34, cx+u*.62, cy+u*.46)
            d.rounded_rectangle(box, radius=max(1, round(u*.12)), outline=alpha, width=stroke)
            d.line((cx-u*.56, cy-u*.34, cx-u*.18, cy-u*.34, cx-u*.02, cy-u*.52,
                    cx+u*.38, cy-u*.52), fill=alpha, width=stroke, joint="curve")
        elif kind == "Image":
            box = (cx-u*.56, cy-u*.48, cx+u*.56, cy+u*.48)
            d.rounded_rectangle(box, radius=max(1, round(u*.12)), outline=alpha, width=stroke)
            d.ellipse((cx-u*.33, cy-u*.28, cx-u*.12, cy-u*.07), outline=alpha, width=stroke)
            d.line((cx-u*.45, cy+u*.30, cx-u*.10, cy-u*.02, cx+u*.08, cy+u*.15,
                    cx+u*.42, cy-u*.22), fill=alpha, width=stroke, joint="curve")
        elif kind == "Text":
            box = (cx-u*.45, cy-u*.55, cx+u*.45, cy+u*.55)
            d.rounded_rectangle(box, radius=max(1, round(u*.13)), outline=alpha, width=stroke)
            for y, length in ((-.22, .38), (0, .38), (.22, .23)):
                d.line((cx-u*length, cy+u*y, cx+u*length, cy+u*y), fill=alpha, width=stroke)
        else:
            d.rounded_rectangle((cx-u*.46, cy-u*.50, cx+u*.46, cy+u*.50),
                                radius=max(1, round(u*.16)), outline=alpha, width=stroke)
            d.line((cx-u*.25, cy-u*.18, cx+u*.25, cy-u*.18), fill=alpha, width=stroke)

    def _clipboard_card(self, clipboard):
        """A task-first clipboard surface with the calculator's density and hierarchy."""
        S, W = self.S, self.w
        tab = getattr(clipboard, "tab", "recent") if clipboard else "recent"
        rows = clipboard.page_items(5) if clipboard else []
        all_rows = clipboard.items_for_tab() if clipboard else []
        marked = clipboard.selected_items() if clipboard else []
        marked_ids = {entry["id"] for entry in marked}
        multi = len(marked) > 1
        selected = (marked[0] if len(marked) == 1 else
                    (clipboard.item(visible_only=True) if clipboard and not marked else None))
        header = self.tool_header_layout()
        self.glass_button("clip:refresh", header["leading"], 36*S, 17*S, "\uE72C", 13, always=True)
        self.label(header["title"], 36*S, "Clipboard", 16, "Semibold Text", 245, header["title_anchor"])
        self.glass_button("tool:minimize", header["minimize"], 36*S, 15*S, "\uE73F", 12, always=True)
        self.glass_button("tool:close", header["close"], 36*S, 15*S, "\uE711", 12, always=True)
        self.segmented("cliptab", (("recent", "Recent"), ("pinned", "Pinned")), tab,
                       (24*S, 70*S, W-24*S, 106*S), 13)

        self.label(24*S, 132*S, "Current clipboard", 12, "Regular", 205, "lm")
        preview = (24*S, 150*S, W-24*S, 296*S)
        self.static(preview, 24*S, strength=5*S, bevel=15*S, rim=.62,
                    frost=max(.55, self.tool_glossiness), lift=.14, raised=1.0)
        if multi:
            kinds = {item.get("kind") for item in marked}
            mark_kind = "Files" if kinds == {"Files"} else "Text" if kinds == {"Text"} else "Other"
            self._clipboard_mark(49*S, 183*S, mark_kind, 235, 18)
            self.label(76*S, 173*S, f"{len(marked)} SELECTED", 11, "Semibold Text", 220, "lm")
            if kinds == {"Files"}:
                total = sum(len(item.get("content", [])) for item in marked)
                detail = f"{total} files ready to paste together"
            elif kinds == {"Text"}:
                total = sum(len(str(item.get("content", ""))) for item in marked)
                detail = f"{total:,} characters joined as one paste"
            else:
                detail = "Select only text or files to paste them together"
            self.label(76*S, 208*S, self.fit(detail, 15, "Semibold Text", W-112*S),
                       15, "Semibold Text", 245, "lm")
            self.label(76*S, 237*S, "Tap a row to focus one item again.", 12, "Regular", 185, "lm")
            if clipboard and clipboard.can_copy_items(marked):
                self.tool_button("clip:copyselected", W-92*S, 267*S, 17*S,
                                 text=f"Copy {len(marked)}", width=92*S, height=34*S, size=11)
            else:
                self.label(W-28*S, 267*S, "Mixed types", 11, "Regular", 185, "rm")
        elif selected:
            kind = selected.get("kind", "Other")
            # Pictures (copied images or image files) get a real thumbnail on
            # the left; the details move beside it.
            has_pic = self._clip_thumb(clipboard, selected, (36*S, 162*S, 148*S, 284*S), 16*S, contain=True)
            tx = 160*S if has_pic else 76*S
            if not has_pic:
                self._clipboard_mark(49*S, 183*S, kind, 235, 18)
            self.label(tx, 173*S, kind.upper(), 11, "Semibold Text", 220, "lm")
            content = selected.get("content", "")
            if kind == "Text":
                # Show the actual words, wrapped over three lines, so a glance
                # tells what was copied.
                flat = " ".join(str(content)[:600].split()) or "Empty text"
                wrapped = self.wrap(flat, 13, "Regular", W - 112*S)
                if len(wrapped) > 3 or len(str(content)) > 600:
                    wrapped = wrapped[:2] + [self.fit(" ".join(wrapped[2:]) + " …", 13, "Regular", W - 112*S)]
                for index, line in enumerate(wrapped[:3]):
                    self.label(76*S, (195 + index*20)*S, line, 13, "Regular", 240, "lm")
                raw_lines = []
                meta = f"{len(str(content)):,} characters"
            elif kind == "Files":
                raw_lines = [os.path.basename(path) or path for path in list(content)[:2]] or ["No files"]
                meta = f"{len(list(content))} file" + ("s" if len(list(content)) != 1 else "")
            elif kind == "Image":
                dimensions = selected.get("dimensions") or ()
                size = int(selected.get("byte_size", len(content)) or 0)
                if size >= 1024 * 1024:
                    size_label = f"{size / (1024 * 1024):.1f} MB"
                else:
                    size_label = f"{max(1, size // 1024)} KB" if size else "Live image"
                resolution = f"{dimensions[0]:,} × {dimensions[1]:,}" if len(dimensions) == 2 else "Image"
                if clipboard and clipboard.can_copy(selected):
                    raw_lines = [resolution, "Cached locally for this Blob session."]
                    meta = size_label
                else:
                    raw_lines = [resolution, "Use the source app's Paste command."]
                    meta = "Live image only"
            else:
                raw_lines = ["Clipboard content is not readable yet"]
                meta = "Unsupported format"
            width = W - tx - 36*S
            for index, line in enumerate(raw_lines[:2]):
                self.label(tx, ((200 + index*22) if has_pic else (204 + index*24))*S, self.fit(str(line), 16 if index == 0 else 13,
                           "Semibold Text" if index == 0 else "Regular", width),
                           16 if index == 0 else 13, "Semibold Text" if index == 0 else "Regular", 248 if index == 0 else 195, "lm")
            stamp = time.strftime("%H:%M", time.localtime(selected.get("time", time.time())))
            meta_w = (W - 148*S - tx) if has_pic else W - 112*S
            self.label(tx, (262 if kind == "Text" else 255)*S if not has_pic else 248*S,
                       self.fit(f"{meta} · {stamp}", 11, "Regular", meta_w), 11, "Regular", 180, "lm")
            ident = selected["id"]
            if clipboard and clipboard.can_copy(selected):
                self.tool_button("clip:copy:"+ident, W-102*S, 267*S, 17*S, text="Copy", width=72*S, height=34*S, size=12)
            else:
                self.label(W-28*S, 267*S, "Live only", 11, "Regular", 185, "rm")
            self.tool_button("clip:pin:"+ident, W-47*S, 267*S, 17*S, icon="\uE718", size=12,
                             tint=(1, 1, 1, .16) if selected.get("pinned") else None)
        else:
            self._clipboard_mark(W/2, 196*S, "Text", 160, 22)
            empty = "Nothing pinned yet" if tab == "pinned" else "Copy text, files, or an image to begin"
            self.label(W/2, 231*S, empty, 14, "Regular", 220, "mm")
            self.label(W/2, 255*S, "Clipboard history stays on this device for this session.",
                       11, "Regular", 170, "mm")

        heading = "Pinned" if tab == "pinned" else "Recent"
        self.label(24*S, 326*S, heading, 17, "Semibold Text", 245, "lm")
        if all_rows:
            self.label(W-24*S, 326*S, f"{len(all_rows)} item" + ("s" if len(all_rows) != 1 else ""),
                       11, "Regular", 185, "rm")
        for index, item in enumerate(rows):
            y = (346 + index*60)*S
            rect = (24*S, y, W-24*S, y+52*S)
            active = item["id"] in marked_ids
            self.static(rect, 18*S, strength=(7 if active else 4)*S, bevel=11*S,
                        rim=.74 if active else .44, frost=max(.50, self.tool_glossiness-.12),
                        lift=.16 if active else .08, raised=1.0 if active else .55)
            key = "clip:select:"+item["id"]
            self.rects[key] = rect
            self.controls.append(("hover", key, rect, 18*S))
            if not self._clip_thumb(clipboard, item, (31*S, y+10*S, 63*S, y+42*S), 9*S):
                self._clipboard_mark(47*S, y+26*S, item.get("kind", "Other"), 220, 14)
            content = item.get("content", "")
            if item.get("kind") == "Text":
                summary = " ".join(str(content).split()) or "Empty text"
                meta = f"Text · {len(str(content)):,} characters"
            elif item.get("kind") == "Files":
                summary = item.get("summary", "Files")
                count = len(list(content))
                meta = f"Files · {count} item" + ("s" if count != 1 else "")
            else:
                summary = item.get("summary", "Clipboard item")
                if item.get("kind") == "Image":
                    dimensions = item.get("dimensions") or ()
                    detail = f"{dimensions[0]} × {dimensions[1]}" if len(dimensions) == 2 else "Image"
                    meta = detail + (" · cached" if clipboard and clipboard.can_copy(item) else " · live")
                else:
                    meta = item.get("kind", "Other")
            self.label(72*S, y+17*S, self.fit(summary, 13, "Semibold Text", W-194*S),
                       13, "Semibold Text", 245, "lm")
            self.label(72*S, y+37*S, meta, 10, "Regular", 185, "lm")
            mark_cx, mark_r = W-88*S, 13*S
            mark_rect = (mark_cx-mark_r, y+26*S-mark_r, mark_cx+mark_r, y+26*S+mark_r)
            self.static(mark_rect, mark_r, strength=6*S, bevel=9*S, rim=.66,
                        frost=max(.62, self.tool_glossiness), lift=.11, raised=1.0)
            self.rects["clip:mark:"+item["id"]] = mark_rect
            self.controls.append(("hover", "clip:mark:"+item["id"], mark_rect, mark_r))
            if item["id"] in marked_ids:
                self.di.text((mark_cx, y+26*S), "\uE73E", font=self.f.icon(10), fill=245, anchor="mm")
            self.tool_button("clip:pin:"+item["id"], W-48*S, y+26*S, 15*S, icon="\uE718", size=10,
                             tint=(1, 1, 1, .16) if item.get("pinned") else None)
        if not rows:
            self.label(W/2, 392*S, "Your pinned clipboard items will appear here.",
                       13, "Regular", 185, "mm")
        pages = clipboard.page_count(5) if clipboard else 1
        page = getattr(clipboard, "page", 0) + 1 if clipboard else 1
        if pages > 1:
            self.tool_button("clip:prev", 44*S, 682*S, 17*S, icon="\uE76B", size=12)
            self.tool_button("clip:next", W-44*S, 682*S, 17*S, icon="\uE76C", size=12)
            self.label(W/2, 682*S, f"{page} / {pages}", 12, "Regular", 210, "mm")
            if tab == "recent":
                self.tool_button("clip:clear", W-96*S, 682*S, 17*S, text="Clear", width=58*S, height=34*S, size=10)
        elif tab == "recent" and all_rows:
            self.tool_button("clip:clear", W-72*S, 682*S, 17*S, text="Clear", width=78*S, height=34*S, size=11)
        status = getattr(clipboard, "status", "Watching clipboard on this device") if clipboard else "Clipboard is loading"
        self.label(24*S, 724*S, self.fit(status, 11, "Regular", W-48*S), 11, "Regular", 185, "lm")

    def _blank_tool_card(self):
        """An intentionally quiet workspace reserved for the next Blob tool."""
        S, W = self.S, self.w
        header = self.tool_header_layout(leading=False)
        self.label(header["title"], 36*S, "New tool", 16, "Semibold Text", 245, header["title_anchor"])
        self.glass_button("tool:minimize", header["minimize"], 36*S, 15*S, "\uE73F", 12, always=True)
        self.glass_button("tool:close", header["close"], 36*S, 15*S, "\uE711", 12, always=True)
        canvas = (24*S, 76*S, W-24*S, self.TOOL_H*S-24*S)
        self.static(canvas, 28*S, strength=4*S, bevel=15*S, rim=.54,
                    frost=max(.22, self.tool_glossiness-.46), lift=.10, raised=.72)
        # A single, passive add mark makes the empty page read as a slot to
        # grow into without pretending a feature already exists.
        r, cx, cy = 18*S, W/2, (76 + (self.TOOL_H-100)/2)*S
        self.static((cx-r, cy-r, cx+r, cy+r), r, strength=5*S, bevel=10*S,
                    rim=.70, frost=max(.62, self.tool_glossiness), lift=.12, raised=.85)
        arm = 6*S
        width = max(1, round(1.25*S))
        self.di.line((cx-arm, cy, cx+arm, cy), fill=205, width=width)
        self.di.line((cx, cy-arm, cx, cy+arm), fill=205, width=width)

    @property
    def magnifier_active(self):
        return self.tools_open and self.tool_view == "magnifier"

    def magnifier_aperture(self):
        return tuple(v * self.S for v in (24, 24, 232, 232))

    def _magnifier_card(self):
        # The GPU draws a circular lens fused to a diagonal frosted rod. Keep
        # buttons and labels off the reading aperture; wheel/right-click control it.
        S = self.S
        self.label(262*S, 262*S, f"{self.magnifier_zoom:g}×", 13,
                   "Semibold Text", 245, "mm")

    ROW_SEARCH, ROW_LIST = 50, 38

    def _backdrop(self, art, H):
        """Cache album colours; the GPU turns them into a full moving colour field."""
        key = (id(art), getattr(art, "size", None))
        if key != getattr(self, "_backdrop_art_key", None):
            self._backdrop_art_key = key
            self._backdrop_palette_cache = album_palette(art)
        self.backdrop_palette = self._backdrop_palette_cache

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
        """Regular cover-first player, with optional bubble and volume utilities."""
        S, W = self.S, self.w
        a = round(W - 2*pad)
        self._artwork(m, round(pad), round(top), a, self.inner_radius(pad), 44)
        # Keep the shared control on top of the artwork's glass so it wins hit
        # testing while retaining the same top-right anchor as other pages.
        self.view_bubble_button("mview:bubble", top)
        # Reserve the narrow control rail in hit testing; the cover still
        # renders edge-to-edge, but a click there belongs unambiguously to the
        # minimize control rather than opening the cover view.
        art_box = self.rects.get("mview:art")
        if art_box:
            if self.anchor_side == "left":
                self.rects["mview:art"] = (art_box[0] + 32 * S, art_box[1],
                                            art_box[2], art_box[3])
            else:
                self.rects["mview:art"] = (art_box[0], art_box[1],
                                            art_box[2] - 32 * S, art_box[3])
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
        """Regular proportions: square cover, clear seek line and filled transport."""
        S, W = self.S, self.w
        a = round(54*S)
        # Mirror the compact composition when parked on the left. The album art
        # stays away from the edge rail, leaving the bubble control visible on
        # the side it will minimize toward instead of placing it over the cover.
        left_rail = self.anchor_side == "left"
        art_x = round(W - pad - a) if left_rail else round(pad)
        self._artwork(m, art_x, round(top+2*S), a, 6*S, 20)
        if left_rail:
            tx, right = pad + 38*S, art_x - 12*S
            search_x, queue_x = pad + 50*S, pad + 16*S
        else:
            tx, right = pad + a + 12*S, W - pad - 44*S
            search_x, queue_x = W - pad - 50*S, W - pad - 16*S
        self.label(tx, top+8*S, self.fit(m.title if m and m.active else "Not playing", 13,
                   "Semibold Text", right-tx), 13, "Semibold Text", 255)
        self.label(tx, top+26*S, self.fit((m.artist or m.source) if m and m.active else "Search to start",
                   11, "Regular", right-tx), 11, "Regular", 190)
        self.view_bubble_button("mview:bubble", top, radius=18, glyph_size=13)
        self.glass_button("mview:search", search_x, top+52*S, 12*S, "\uE721", 10)
        self.glass_button("mview:queue", queue_x, top+52*S, 12*S, "\uE8FD", 10)
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
        main = self.bubble_main_box()
        self.rects["mbubble:gesture"] = main
        self.controls.append(("hover", "mbubble:gesture", main, 39 * S))
        self._bubble_restore("mview:now")
        cx, cy, u = self._bubble_main_center(), 55 * S, 7.8 * S
        if m and m.playing:
            bw, bh = 2.7 * S, 7.8 * S
            for sx in (-1, 1):
                x = cx + sx * 4.1 * S
                self.di.rounded_rectangle((x - bw / 2, cy - bh, x + bw / 2, cy + bh),
                                          radius=bw / 2, fill=235)
        else:
            self.di.polygon([(cx - u * .55, cy - u), (cx - u * .55, cy + u),
                             (cx + u * .95, cy)], fill=235)
        self._draw_tool_satellites()

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
        metric_pad = self.edge_safe_pad(pad)
        col2 = W / 2 + 4 * S
        L(metric_pad, px(2), "CPU", 11)
        L(metric_pad, px(14), fmt_temp(cpu["temp"]), 30, "Semibold Display", 255 if cpu["temp"] else 105,
          temp=cpu["temp"])
        L(metric_pad, px(54), ("Fan " + fmt_rpm(fans[0]["rpm"])) if fans else "no fan data", 11)
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
        self.view_bubble_button("hview:bubble", top)
        self.glass_button("details", W-pad-42*S, px(116), 13*S,
                          "" if self.details_open else "", 9, always=True)

    def _sound_compact(self, snd, pad, top):
        """On/off, boost and the spectrum — the rest lives in the regular size."""
        S, W = self.S, self.w
        px = lambda v: top + v * S
        L = self.label
        left = self.edge_safe_pad(pad)
        toggle_x = W - pad - 84 * S
        L(left, px(12), "Boost", 13, "Semibold Text", 255, "lm")
        L(toggle_x - 8 * S, px(12), f"+{snd.boost_db:.0f} dB", 12, "Regular", 175, "rm")
        self.toggle("sound", snd.enabled, toggle_x, px(-2))
        self.view_bubble_button("sview:bubble", top)
        self.slider("boost", snd.boost, pad + 12 * S, W - pad - 12 * S, px(48))
        if snd.cable is False:
            self.controls.append(("viz", (pad, px(70), W - pad, px(85))))
            bx = (pad, px(96), W - pad, px(126))
            self.rects["setupaudio"] = bx
            self.static(bx, 15 * S, strength=5 * S, bevel=8 * S, zoom=0.96,
                        rim=0.9, frost=1.0, lift=0.12, raised=1.0)
            L((bx[0] + bx[2]) / 2, px(111), "Set up system-wide audio", 11,
              "Semibold Text", 255, "mm")
        elif snd.fx_conflict:
            self.controls.append(("viz", (pad, px(70), W - pad, px(85))))
            L(pad, px(91), "FxSound is active", 11, "Regular", 200)
            bx = (pad, px(106), W - pad, px(136))
            self.rects["fxquit"] = bx
            self.static(bx, 15 * S, strength=5 * S, bevel=8 * S, zoom=0.96,
                        rim=0.9, frost=1.0, lift=0.16, raised=1.0)
            L((bx[0] + bx[2]) / 2, px(121), "Use Blob audio", 11,
              "Semibold Text", 255, "mm")
        elif snd.error:
            self.controls.append(("viz", (pad, px(70), W - pad, px(120))))
            for i, line in enumerate(self.wrap(snd.error, 11, "Regular", W - 2 * pad)[:2]):
                L(pad, px(92 + i * 15), line, 11)
        else:
            self.controls.append(("viz", (pad, px(70), W - pad, px(120))))

    def _blob(self, s, pad, top):
        S, W = self.S, self.w
        px = lambda v: top + v * S
        cpu, gpu, fans = s["cpu"], s["gpu"], s["fans"]
        metric_pad = self.edge_safe_pad(pad)
        col2 = W / 2 + 6 * S
        L = self.label
        # Keep the details action visible as a real nested control; the old
        # hover-only lens nearly vanished against the bottom edge.
        self.view_bubble_button("hview:bubble", top)

        L(metric_pad, px(4), "CPU", 11, "Semibold Text", 165)
        L(metric_pad, px(20), fmt_temp(cpu["temp"]), 38, "Semibold Display",
          255 if cpu["temp"] is not None else 105, temp=cpu["temp"])
        fan0 = fmt_rpm(fans[0]["rpm"]) if fans else "No fan sensor"
        L(metric_pad, px(66), fan0, 11, "Regular", 155)
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
            self.glass_button("details", W - pad - 14 * S, chevron_y, 13 * S,
                              "" if self.details_open else "", 9, always=True)

    def _sound(self, snd, pad, top):
        S, W = self.S, self.w
        px = lambda v: top + v * S
        L = self.label
        left = self.edge_safe_pad(pad)
        toggle_x = W - pad - 88 * S
        selector_right = toggle_x - 10 * S
        L(left, px(6), "Output", 12)
        self.hover_lens("device", (left - 6 * S, px(20), selector_right, px(44)), 12 * S)
        destination = short_device(snd.output)
        route = f"Blob · {destination}" if destination != "No output" else "Blob"
        L(left, px(32), self.fit(route + "  ›", 14, "Semibold Text",
                              selector_right - left - 12 * S), 14, "Semibold Text", 255, "lm")
        self.toggle("sound", snd.enabled, toggle_x, px(10))
        self.view_bubble_button("sview:bubble", top)
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
            L((bx[0] + bx[2]) / 2, px(430), "Use Blob audio", 12, "Semibold Text", 255, "mm")
            for i, line in enumerate(self.wrap("FxSound is active. Blob needs to take over the audio route.", 12, "Regular",
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
        if self.sound_menu:
            self._sound_output_menu(snd, pad, top)

    def _sound_output_menu(self, snd, pad, top):
        """Show physical destinations as selectable rows instead of cycling on click."""
        S, W = self.S, self.w
        px = lambda v: top + v * S
        left = self.edge_safe_pad(pad) - 4 * S
        right = W - pad - 22 * S
        names = list(getattr(snd, "outputs", []) or [])
        if not names and getattr(snd, "output", None):
            names = [snd.output]
        visible = names[:4]
        row_h = 28 * S
        top_y = px(48)
        bottom_y = top_y + (len(visible) * row_h) + 8 * S
        if not visible:
            visible = [None]
            bottom_y = top_y + row_h + 8 * S
        self.static((left, top_y, right, bottom_y), 14 * S,
                    strength=7 * S, bevel=9 * S, rim=0.7, frost=1.0, lift=0.08, raised=1.0)
        for i, name in enumerate(visible):
            y0 = top_y + 4 * S + i * row_h
            row = (left + 4 * S, y0, right - 4 * S, y0 + row_h - 2 * S)
            self.rects[f"output:{i}"] = row
            if name is not None and name == getattr(snd, "output", None):
                self.static(row, (row[3] - row[1]) / 2, strength=4 * S, bevel=6 * S,
                            rim=0.45, frost=0.7, lift=0.04, raised=0.7)
                text = "✓ " + short_device(name)
            elif name is not None:
                text = short_device(name)
            else:
                text = "No speaker destinations found"
            text = self.fit(text, 11, "Semibold Text" if name == getattr(snd, "output", None)
                             else "Regular", row[2] - row[0] - 16 * S)
            self.label(row[0] + 9 * S, (row[1] + row[3]) / 2, text, 11,
                       "Semibold Text" if name == getattr(snd, "output", None) else "Regular",
                       245 if name == getattr(snd, "output", None) else 190, "lm")

    def _settings(self, glassiness, startup, pointer, pad, top, captureable=False):
        """Grouped, scrollable settings: each section covers one part of Blob."""
        S, W = self.S, self.w
        rail_pad = self.edge_safe_pad(pad)
        pad = self.pad_u * S
        c = self.compact
        L = self.label
        opts = self.options
        self.view_bubble_button("settingsview:bubble", top)
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
            ("seg", "gview", "Gaming view", GAMING_VIEWS, self.gaming_view),
            ("note", "overlay_info", "One lock state is shared by every tab and only changes when you use the shortcut or tray command.", None, None),
            ("note", "gaming_info", self.game.get("status", "Open Gaming for FPS, frame time and system stats."), None, None),
            ("note", "gaming_tip", f"Blob stays unlocked across tabs until you use {self.hotkey_label} to lock it. While locked it passes clicks through; in Gaming, hold the shortcut modifiers to drag temporarily.", None, None),
            ("note", "overlay", f"{self.hotkey_label} locks or unlocks Blob in every view.", None, None),
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
        view_h = self.height(None) - top - 56 * S
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
                row_pad = rail_pad if self.anchor_side == "left" and index == first else pad
                if kind == "head":
                    L(row_pad, y + 14 * S, key.upper(), 10, "Semibold Text", 195, "lm")
                    self.di.rectangle((pad, y + 24 * S, W - pad, y + 24 * S + max(1, round(S)) - 1), fill=32)
                elif kind == "note":
                    for i, line in enumerate(names):
                        L(row_pad, y + (10 + i * 16) * S, line, 11, "Regular", 205, "lm")
                elif kind == "slider":
                    _, _, name, val, hint = row
                    L(row_pad, y + 10 * S, name, 13 if c else 14, "Semibold Text", 255, "lm")
                    L(W - pad, y + 10 * S, hint, 12, "Regular", 165, "rm")
                    self.slider(key, val, row_pad + 12 * S, W - row_pad - 12 * S, y + 38 * S)
                elif kind == "seg":
                    _, _, name, options, cur = row
                    L(row_pad, y + 14 * S, name, 13 if c else 14, "Semibold Text", 255, "lm")
                    self.segmented(key, options, cur, (row_pad, y + 26 * S, W - row_pad, y + 54 * S), 11)
                elif kind == "keybind":
                    _, _, name, binding, hint = row
                    for i, line in enumerate(names):
                        L(row_pad, y + (16 + i * 18) * S, line, 13 if c else 14, "Regular", 255, "lm")
                    text = "Press keys…" if self.hotkey_editing else binding
                    by = y + (20 + len(names)*18)*S
                    box = (row_pad, by, W-row_pad, by+30*S)
                    self.static(box, 15 * S, strength=5 * S, bevel=8 * S, zoom=.97,
                                rim=.8, frost=1.0, lift=.10, raised=.7)
                    self.hover_lens("hotkey:overlay", box, 15 * S)
                    L((box[0] + box[2]) / 2, (box[1] + box[3]) / 2,
                      self.fit(text, 11, "Semibold Text", box[2] - box[0] - 14 * S),
                      11, "Semibold Text", 255, "mm")
                    for i, line in enumerate(hints):
                        L(row_pad, by + (44 + i * 14) * S, line, 10, "Regular",
                          235 if self.hotkey_error else 205, "lm")
                else:
                    _, _, name, val, hint = row
                    for i, line in enumerate(names):
                        L(row_pad, y + (16 + i * 18) * S, line, 13 if c else 14, "Regular", 255, "lm")
                    for i, line in enumerate(hints):
                        L(row_pad, y + (24 + len(names) * 18 + i * 14) * S, line, 10, "Regular", 205, "lm")
                    self.toggle(key, val, W - row_pad - 48 * S, y + 4 * S)
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
        self.tools = ToolController()
        self.clipboard = ClipboardController()
        self._clipboard_poll_at = 0.0
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
        self.panel_side = "right"
        self.panel.anchor_side = self.panel_side
        self.panel.music_view = os.environ.get("BLOB_MVIEW", "now")      # debug hooks
        if os.environ.get("BLOB_QUERY"):
            self.panel.query = os.environ["BLOB_QUERY"]
            self.am.search(self.panel.query)
        # the window buffer is always sized for the wide panel; compact just draws narrower inside it
        self.ss = Panel.SS
        self.glass = glass.GlassRenderer(S, round(Panel.GAMING_WIDE * S), round(self.panel.max_height() / self.ss))
        self.glass.set_supersample(self.ss)
        self.glass.set_panel_side(self.panel_side)
        cfg = engine.load_config()
        if "BLOB_MVIEW" not in os.environ:
            # A bubble is an in-session presentation, not a startup state.  A
            # fresh launch should always land on the full Music card so the
            # user can configure playback before collapsing it manually.
            self.panel.music_view = "now"
            if cfg.get("music_view") != "now":
                cfg["music_view"] = "now"
                engine.save_config(cfg)
        self.glassiness = float(cfg.get("glass", 0.35))
        self.captureable = bool(cfg.get("captureable", False))
        self.panel.set_compact(os.environ.get("BLOB_COMPACT", "1" if cfg.get("compact") else "0") == "1")
        self.panel.hardware_view = os.environ.get("BLOB_HARDWARE_VIEW", cfg.get("hardware_view", "card"))
        if self.panel.hardware_view not in ("card", "bubble"):
            self.panel.hardware_view = "card"
        self.panel.sound_view = os.environ.get("BLOB_SOUND_VIEW", cfg.get("sound_view", "card"))
        if self.panel.sound_view not in ("card", "bubble"):
            self.panel.sound_view = "card"
        self.panel.hardware_mode = os.environ.get("BLOB_HARDWARE_MODE", cfg.get("hardware_mode", "auto"))
        if self.panel.hardware_mode not in dict(HARDWARE_MODES):
            self.panel.hardware_mode = "auto"
        self.panel.backdrop = bool(cfg.get("backdrop", False))
        stored_gaming_view = cfg.get("gaming_view", "horizontal")
        self.panel.gaming_view = normalize_gaming_view(stored_gaming_view)
        stored_gaming_restore = normalize_gaming_view(cfg.get("gaming_restore_view", "horizontal"))
        if stored_gaming_restore == "bubble":
            stored_gaming_restore = "horizontal"
        self.panel.gaming_restore_view = (
            stored_gaming_restore if self.panel.gaming_view == "bubble" else self.panel.gaming_view)
        if stored_gaming_view != self.panel.gaming_view:
            cfg["gaming_view"] = self.panel.gaming_view
            engine.save_config(cfg)
        # The page is loaded before the per-page view. Recompute the width
        # after restoring Gaming so saved Vertical/Bubble is correct on the
        # first frame instead of one frame late.
        self.panel.update_width()
        self.panel.options = dict(cfg.get("options", {}))
        self.hotkey_mods, self.hotkey_vk = parse_hotkey(cfg.get("overlay_hotkey", {}))
        self.panel.hotkey_label = DUAL_CONTROL_LABEL
        self.startup = startup_enabled()
        self.springs = Springs()
        self.springs.get("width", self.panel.w, k=240, zeta=0.90)
        self.springs.get(
            "hardware_morph",
            1.0 if (self.panel.hardware_view == "bubble" or
                    (self.panel.page == "gaming" and self.panel.gaming_view == "bubble")) else 0.0,
                         k=220, zeta=0.86)
        self.springs.get("music_morph", 1.0 if self.panel.music_view == "bubble" else 0.0,
                         k=220, zeta=0.86)
        self.springs.get("sound_morph", 1.0 if (self.panel.page == "sound" and
                                                 self.panel.sound_view == "bubble") else 0.0,
                                                 k=220, zeta=0.86)
        self.springs.get("settings_morph", 1.0 if (self.panel.page == "settings" and
                                                    self.panel.settings_view == "bubble") else 0.0,
                                                    k=220, zeta=0.86)
        self.springs.get("bubble_proximity", 0.0,
                         k=BUBBLE_PROXIMITY_K, zeta=BUBBLE_PROXIMITY_ZETA)
        self.springs.get("music_bass", 0.0, k=150, zeta=.72)
        self.springs.get("music_mid", 0.0, k=210, zeta=.78)
        self.springs.get("music_treble", 0.0, k=280, zeta=.82)
        self.music_peak_prev = 0.0
        # Bubble mode occupies the card's former top-right corner. The fixed
        # layered window has room below it, so expansion can grow back down.
        self.hardware_top = self.glass.panel_y(round((210 + Panel.TOP) * self.S))
        self.music_top = self.glass.panel_y(round((Panel.TOP + 118) * self.S))
        sound_card_h = Panel.TOP + (150 if self.panel.compact else 462)
        self.sound_top = self.glass.panel_y(round(sound_card_h * self.S))
        settings_card_h = Panel.TOP + (300 if self.panel.compact else 420)
        self.settings_top = self.glass.panel_y(round(settings_card_h * self.S))
        self.gaming_top = self.gaming_anchor()
        self.controls, self.old_controls = [], []
        self.visible, self.pinned = False, True
        self.audio_motion = AudioMotion()
        self.backdrop_started = time.perf_counter()
        self._overlay_unlocked = True
        self.gaming_modifier_drag = False
        # Edge docking is live placement state. A free drag clears it; a
        # deliberate monitor-edge drop establishes it again.
        self.gaming_dock_edge = None
        self.hotkey_editing = False
        self.hotkey_swallow = set()
        self.dual_control_down = {LEFT_CONTROL: False, RIGHT_CONTROL: False}
        self.dual_control_latched = False
        if PROFILE is not None:
            self.pinned = True  # profiling: keep the panel up even when focus moves elsewhere
        self.pos = None  # window top-left (screen px)
        self.drag = None
        self.drag_click = None
        self.drag_origin = None
        self.drag_moved = False
        self.drag_work = None
        self.music_press_active = False
        self.music_hold_fired = False
        self.music_click_pending = False
        self.music_click_deadline = 0.0
        self.slider_drag = None
        self.pressed = None
        self.hover = None
        self.mouse_in = False
        self.mouse_xy = None
        self.bubble_linger_until = 0.0
        self.tool_top = None
        self.attached = self.detaching = None
        self.pointer_style = cfg.get("pointer", "arrow")
        self.last_hide = 0.0
        self.snap = None
        self.light = np.array([-0.55, -0.83])
        self.vel = np.zeros(2)
        self.last_frame = time.perf_counter()
        self.last_text = 0.0
        self._tabs_texture_at = 0.0
        self.interval = 0
        self.frame_dirty = True
        # The process is per-monitor DPI aware; use the actual host monitor
        # before the first reveal instead of assuming the primary display.
        self.sync_dpi(self.window_dpi())

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
        # The lens follows the real pointer, so leaving the pointer visible
        # would draw it directly in the middle of the reading aperture.
        and_mask = (ctypes.c_ubyte * 128)(*([0xFF] * 128))
        xor_mask = (ctypes.c_ubyte * 128)(*([0x00] * 128))
        self.cur_blank = user32.CreateCursor(None, 0, 0, 32, 32, and_mask, xor_mask)
        self.apply_gaming_input()
        # RegisterHotKey cannot distinguish the physical left and right Ctrl
        # keys and can be claimed by a game. The global low-level hook below
        # handles the dedicated two-control chord instead.
        self.gaming_hotkey_registered = False

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

    def _update_tabs_side(self):
        """Keep the Gaming menu on the edge where SmallBlob is parked.

        The renderer owns a fixed, shadowed window and right-aligns the actual
        panel inside it, so using only ``self.pos`` would give the wrong answer
        for compact panels.  Resolve the panel's real screen centre against
        the work area of the monitor it occupies instead.
        """
        if self.pos is None:
            x, y = cursor_pos()
        else:
            width = self.springs.get("width", self.panel.w).x / self.ss
            height = self.springs.get("height", self.panel.height(self.snap)).x / self.ss
            x = self.pos[0] + self.glass.panel_x(width) + width / 2
            y = self.pos[1] + self._panel_y(height) + height / 2
        work, _ = work_area_at(round(x), round(y))
        side = "left" if x <= (work.left + work.right) / 2 else "right"
        if side == getattr(self.panel, "tabs_side", "right"):
            return False
        self.panel.tabs_side = side
        return True

    def _panel_screen_rect(self, width=None, height=None):
        """Resolve the drawn panel, not the fixed layered-window buffer, in screen px."""
        if (self.pos is None or not hasattr(self, "glass") or not hasattr(self, "ss") or
                not hasattr(self.panel, "w")):
            return None
        width = (self.springs.get("width", self.panel.w).x / self.ss
                 if width is None else float(width))
        height = (self.springs.get("height", self.panel.height(self.snap)).x / self.ss
                  if height is None else float(height))
        px = self.glass.panel_x(width)
        py = self._panel_y(height)
        return (self.pos[0] + px, self.pos[1] + py, width, height)

    def _panel_work_area(self):
        """Resolve the monitor from the visible panel centre, never its buffer."""
        rect = self._panel_screen_rect()
        if rect is not None:
            x, y, width, height = rect
            return work_area_at(round(x + width / 2), round(y + height / 2))[0]
        x, y = cursor_pos()
        return work_area_at(x, y)[0]

    def _drag_work_for_cursor(self, x, y):
        """Transfer monitors only after the pointer is clearly past the old edge.

        Immediate monitor changes at a shared edge were the source of the
        half-clipped, jumping drag.  Keeping one work area until the pointer
        has crossed a small gutter makes the transfer deterministic, while a
        later clamp guarantees the complete panel lands on the new display.
        """
        current = getattr(self, "drag_work", None) or self._panel_work_area()
        candidate, _ = work_area_at(round(x), round(y))
        if work_rect_key(candidate) != work_rect_key(current):
            gutter = max(16.0, DRAG_MONITOR_TRANSFER_DIP * max(1.0, float(self.S)))
            if point_outside_work_area(x, y, current) >= gutter:
                self.drag_work = candidate
                return candidate
        self.drag_work = current
        return current

    def keep_panel_in_work_area(self, screen_hint=None, work=None):
        """Keep the visible glass, controls and shadow inside one work area.

        This is intentionally based on the drawn panel rather than the fixed
        layered-window canvas.  It runs during size springs as well as drags,
        so expanding a card, tool or Gaming strip cannot retain an old
        off-screen origin and lose its action rail.
        """
        if self.pos is None or not hasattr(self, "glass"):
            return False
        rect = self._panel_screen_rect()
        if rect is None:
            return False
        x, y, width, height = rect
        if work is None:
            if screen_hint is None:
                monitor_x, monitor_y = x + width / 2, y + height / 2
            else:
                monitor_x, monitor_y = screen_hint
            work, _ = work_area_at(round(monitor_x), round(monitor_y))
        # Keep one actual pixel between the soft shadow and the work-area
        # boundary. It prevents anti-aliased edge clipping on fractional DPI.
        guard = float(self.glass.sp + max(1, round(float(self.S))))
        panel_x, panel_y = self.glass.panel_x(width), self._panel_y(height)
        x_low = work.left - (panel_x - guard)
        x_high = work.right - (panel_x + width + guard)
        y_low = work.top - (panel_y - guard)
        y_high = work.bottom - (panel_y + height + guard)
        old = tuple(self.pos)
        if x_low <= x_high:
            self.pos[0] = min(max(self.pos[0], x_low), x_high)
        else:
            self.pos[0] = (x_low + x_high) / 2
        if y_low <= y_high:
            self.pos[1] = min(max(self.pos[1], y_low), y_high)
        else:
            self.pos[1] = (y_low + y_high) / 2
        changed = abs(self.pos[0] - old[0]) > .01 or abs(self.pos[1] - old[1]) > .01
        if changed:
            self.frame_dirty = True
            # A clamp is also a physical movement. Rebase the drag offset so
            # the next mouse sample continues from the same point instead of
            # snapping the glass back across the monitor seam.
            if self.drag is not None and screen_hint is not None:
                self.rebase_drag_anchor()
        return changed

    def rebase_drag_anchor(self):
        """Keep a live drag attached to the pointer after DPI/side geometry changes."""
        if self.drag is None or self.pos is None:
            return
        cx, cy = cursor_pos()
        self.drag = (cx - self.pos[0], cy - self.pos[1])

    def set_panel_side(self, side, preserve=True, screen_hint=None, work=None):
        """Move the panel anchor without teleporting its current visible surface."""
        side = "left" if side == "left" else "right"
        screen_hint = screen_hint or (cursor_pos() if self.drag is not None else None)
        if side == self.panel_side:
            changed = self.panel.anchor_side != side
            self.panel.anchor_side = side
            self.glass.set_panel_side(side)
            self.keep_panel_in_work_area(screen_hint, work)
            if screen_hint is not None:
                self.rebase_drag_anchor()
            if changed and getattr(self, "visible", False):
                self.draw_content()
            return changed
        width = self.springs.get("width", self.panel.w).x / self.ss
        old_x = self.pos[0] + self.glass.panel_x(width) if self.pos is not None else None
        self.panel_side = side
        self.panel.anchor_side = side
        self.glass.set_panel_side(side)
        if preserve and self.pos is not None and old_x is not None:
            self.pos[0] += old_x - (self.pos[0] + self.glass.panel_x(width))
        self.keep_panel_in_work_area(screen_hint, work)
        if screen_hint is not None:
            self.rebase_drag_anchor()
        self.frame_dirty = True
        if getattr(self, "visible", False):
            self.draw_content()
        return True

    def prepare_minimize_anchor(self, local_x=None):
        """Choose the destination edge from the actual button the user pressed."""
        if self.pos is None:
            return
        width = self.springs.get("width", self.panel.w).x / self.ss
        if local_x is None or abs(local_x) < 1e-6:
            screen_x, screen_y = cursor_pos()
        else:
            screen_x = round(self.pos[0] + self.glass.panel_x(width) + local_x / self.ss)
            screen_y = round(self.pos[1] + self._panel_y(
                self.springs.get("height", self.panel.height(self.snap)).x / self.ss))
        work, _ = work_area_at(screen_x, screen_y)
        self.set_panel_side("left" if screen_x <= (work.left + work.right) / 2 else "right")

    def maybe_flip_panel_side(self, screen_hint=None, work=None):
        """Follow the actual panel centre, with a stable work-area handoff."""
        rect = self._panel_screen_rect()
        if rect is None:
            return
        x, y, width, height = rect
        anchor_x, anchor_y = x + width / 2, y + height / 2
        if work is None:
            work, _ = work_area_at(round(anchor_x), round(anchor_y))
        side = "left" if anchor_x <= (work.left + work.right) / 2 else "right"
        if side != self.panel_side:
            self.set_panel_side(side, preserve=True, screen_hint=screen_hint, work=work)

    def _draw_content(self, crossfade=False):
        self.gaming.set_active(self.visible and self.panel.page == "gaming")
        self._sync_music_ambient()
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
                                                self.captureable, getattr(self, "tools", None),
                                                getattr(self, "clipboard", None))
        pic = self.panel.pic
        backdrop = getattr(self.panel, "backdrop_layer", None)
        if hasattr(self.glass, "set_backdrop_palette"):
            self.glass.set_backdrop_palette(getattr(self.panel, "backdrop_palette", None))
        if crossfade:
            # the tab bar is shared by every page, so its thumb slides instead of fading
            outgoing = [c for c in self.controls if not (c[0] == "seg" and c[1] == "page")]
            # The game-safe FPS bubble never carries the general tool palette.
            # Drop outgoing palette arcs before crossfading so a page switch
            # cannot leave a malformed halo around the FPS readout.
            if self.panel.page == "gaming":
                outgoing = [c for c in outgoing if c[0] != "arc"]
            self.old_controls = outgoing
            self.glass.set_content(ink, accent, pic, backdrop)
            fade = self.springs.get("fade", 1.0, k=260, zeta=1.0)
            fade.x, fade.v, fade.target = 0.0, 0.0, 1.0
        else:
            self.glass.replace_content(ink, accent, pic, backdrop)
        self.controls = controls
        h = self.springs.get("height", self.panel.height(self.snap), k=320, zeta=0.86)
        h.target = self.panel.height(self.snap)
        w = self.springs.get("width", self.panel.w, k=240, zeta=0.90)
        w.target = self.panel.w
        self.frame_dirty = True

    def _sync_music_ambient(self):
        """Keep album tint in the full music views, never in the transparent bubble."""
        if not hasattr(self, "glass") or not hasattr(self.glass, "set_ambient"):
            return
        art = getattr(getattr(self, "media", None), "art", None)
        if (not getattr(self.panel, "tools_open", False) and self.panel.page == "music" and
                self.panel.music_view != "bubble" and not self.panel.backdrop):
            self.glass.set_ambient(*album_tint(art))
        else:
            self.glass.set_ambient((0, 0, 0, 0))

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
        if hasattr(self, "glass"):
            sound_card_h = Panel.TOP + (150 if compact else 462)
            self.sound_top = self.glass.panel_y(round(sound_card_h * self.S))
            settings_card_h = Panel.TOP + (300 if compact else 420)
            self.settings_top = self.glass.panel_y(round(settings_card_h * self.S))
        self.old_page_springs()
        self.springs.pop("height", None)

    def window_dpi(self):
        """Return the effective DPI of the monitor currently hosting Blob."""
        try:
            if getattr(self, "hwnd", None) and hasattr(user32, "GetDpiForWindow"):
                dpi = int(user32.GetDpiForWindow(self.hwnd))
                if dpi > 0:
                    return dpi
        except (OSError, TypeError, ValueError):
            pass
        return max(96, round(self.S * 96))

    def sync_dpi(self, dpi=None):
        """Keep layout, fonts, glass buffers and the visible panel size per-monitor."""
        target = max(96, int(dpi or self.window_dpi()))
        new_s = target / 96.0
        old_s = float(self.S)
        if abs(new_s - old_s) < 0.005:
            return False
        ratio = new_s / old_s
        old_width = (self.springs["width"].x / self.ss if "width" in self.springs
                     else self.panel.w / self.ss)
        old_height = (self.springs["height"].x / self.ss if "height" in self.springs
                      else self.panel.height(self.snap) / self.ss)
        visible = None
        if self.pos is not None:
            visible = (self.pos[0] + self.glass.panel_x(old_width),
                       self.pos[1] + self._panel_y(old_height))

        self.S = new_s
        panels = [getattr(self, "panel", None)]
        panels.extend(getattr(self, "panels", {}).values())
        seen = set()
        for panel in panels:
            if panel is not None and id(panel) not in seen:
                seen.add(id(panel))
                panel.set_scale(new_s)
        for key in ("width", "height"):
            if key in self.springs:
                spring = self.springs[key]
                spring.x *= ratio
                spring.target *= ratio
                spring.v *= ratio

        # The renderer's scale is part of its shader and its fixed DIB. Resize
        # both together so a monitor change cannot leave a large old surface.
        self.glass.S = new_s
        self.glass.sp = int(round(28 * new_s))
        self.glass.M = int(round(44 * new_s))
        if hasattr(self.glass, "prog"):
            self.glass.prog["S"].value = float(new_s)
            self.glass.prog["panel_r"].value = float(34 * new_s)
            self.glass.prog["ptr_size"].value = float(19 * new_s)
        if hasattr(self.glass, "resize_surface"):
            if getattr(self, "full", False):
                if getattr(self.panel, "dock", False):
                    rw = round(self.panel.DOCK_W * new_s)
                    rh = round(self.panel.DOCK_H * new_s)
                else:
                    rw = round(self.panel.window_w * new_s)
                    rh = round(self.panel.window_h * new_s)
            else:
                rw = round(Panel.GAMING_WIDE * new_s)
                rh = round(self.panel.max_height() / self.ss)
            self.glass.resize_surface(rw, rh)
        self.glass.set_supersample(self.ss)
        self.glass.set_panel_side(self.panel_side)

        self.hardware_top = self.glass.panel_y(round((210 + Panel.TOP) * new_s))
        self.music_top = self.glass.panel_y(round((Panel.TOP + 118) * new_s))
        sound_card_h = Panel.TOP + (150 if self.panel.compact else 462)
        self.sound_top = self.glass.panel_y(round(sound_card_h * new_s))
        settings_card_h = Panel.TOP + (300 if self.panel.compact else 420)
        self.settings_top = self.glass.panel_y(round(settings_card_h * new_s))
        self.gaming_top = self.gaming_anchor()

        if visible is not None:
            new_width = (self.springs["width"].x / self.ss if "width" in self.springs
                         else self.panel.w / self.ss)
            new_height = (self.springs["height"].x / self.ss if "height" in self.springs
                          else self.panel.height(self.snap) / self.ss)
            self.pos[0] = visible[0] - self.glass.panel_x(new_width)
            self.pos[1] = visible[1] - self._panel_y(new_height)
            screen_hint = cursor_pos() if self.drag is not None else None
            self.keep_panel_in_work_area(screen_hint)
            if screen_hint is not None:
                self.rebase_drag_anchor()
        self.old_controls = []
        for key in [key for key in self.springs if key.startswith("old:")]:
            del self.springs[key]
        if self.panel.tools_open and self.panel.tool_view in ("calculator", "clipboard", "blank"):
            self.fit_calculator_in_work_area(cursor_pos() if self.drag is not None else None)
        self.frame_dirty = True
        if getattr(self, "visible", False) or self.snap is not None:
            self.draw_content()
        return True

    def set_hardware_view(self, view):
        if view not in ("card", "bubble") or view == self.panel.hardware_view:
            return
        # Seed every dimension from the currently visible geometry before changing
        # the layout target. This turns the card/bubble switch into one continuous
        # spring motion instead of constructing the destination at full size.
        width = self.springs.get("width", self.panel.w, k=240, zeta=0.90)
        height = self.springs.get("height", self.panel.height(self.snap), k=320, zeta=0.86)
        morph = self.springs.get(
            "hardware_morph", 1.0 if self.panel.hardware_view == "bubble" else 0.0,
            k=220, zeta=0.86)
        # Keep the three geometric channels phase-aligned; otherwise the panel
        # briefly looks merely resized before its outline catches up.
        for spring in (width, height, morph):
            spring.k = 235.0
            spring.c = 2 * 0.86 * spring.k ** 0.5
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

    def set_sound_view(self, view):
        """Select the compact Sound bubble without changing the audio state."""
        if view not in ("card", "bubble") or view == self.panel.sound_view:
            return
        width = self.springs.get("width", self.panel.w, k=240, zeta=.90)
        height = self.springs.get("height", self.panel.height(self.snap), k=320, zeta=.86)
        morph = self.springs.get(
            "sound_morph", 1.0 if self.panel.sound_view == "bubble" else 0.0,
            k=220, zeta=.86)
        if view == "bubble":
            self.sound_top = self.glass.panel_y(round(height.x / self.ss))
        cfg = engine.load_config()
        cfg["sound_view"] = view
        engine.save_config(cfg)
        self.panel.sound_view = view
        self.panel.tabs_open = False
        self.panel.tabs_t = 0.0
        self.springs.pop("tabs", None)
        self.old_page_springs()
        self.panel.update_width()
        for spring in (width, height, morph):
            spring.k = 235.0
            spring.c = 2 * .86 * spring.k ** .5
        width.target = self.panel.w
        height.target = self.panel.height(self.snap)
        morph.target = 1.0 if view == "bubble" else 0.0

    def set_settings_view(self, view):
        """Give Settings the same card-to-bubble transition as every other page."""
        if view not in ("card", "bubble") or view == self.panel.settings_view:
            return
        width = self.springs.get("width", self.panel.w, k=240, zeta=.90)
        height = self.springs.get("height", self.panel.height(self.snap), k=320, zeta=.86)
        morph = self.springs.get(
            "settings_morph", 1.0 if self.panel.settings_view == "bubble" else 0.0,
            k=220, zeta=.86)
        if view == "bubble":
            self.settings_top = self.glass.panel_y(round(height.x / self.ss))
        self.panel.settings_view = view
        self.panel.tabs_open = False
        self.panel.tabs_t = 0.0
        self.springs.pop("tabs", None)
        self.old_page_springs()
        self.panel.update_width()
        for spring in (width, height, morph):
            spring.k = 235.0
            spring.c = 2 * .86 * spring.k ** .5
        width.target = self.panel.w
        height.target = self.panel.height(self.snap)
        morph.target = 1.0 if view == "bubble" else 0.0

    def gaming_anchor(self):
        """Return the bottom-anchored top edge of the active Gaming strip."""
        current = self.panel.gaming_view
        tabs_open, tabs_t = self.panel.tabs_open, self.panel.tabs_t
        source = self.panel.gaming_restore_view if current == "bubble" else current
        self.panel.gaming_view = source if source in ("horizontal", "vertical") else "horizontal"
        self.panel.tabs_open = False
        self.panel.tabs_t = 0.0
        height = self.panel.height(getattr(self, "snap", None), page="gaming")
        self.panel.gaming_view = current
        self.panel.tabs_open, self.panel.tabs_t = tabs_open, tabs_t
        return self.glass.panel_y(round(height / self.ss))

    def _small_gaming_strip_active(self):
        return (not getattr(self, "full", False) and self.panel.page == "gaming" and
                not getattr(self.panel, "tools_open", False) and
                getattr(self.panel, "gaming_view", "horizontal") in ("horizontal", "vertical"))

    def _snap_gaming_strip_to_edge(self, edge, work):
        """Pin the visible Gaming surface to an edge while preserving its shadow."""
        rect = self._panel_screen_rect()
        if rect is None or self.pos is None:
            return False
        x, y, width, _ = rect
        guard = float(self.glass.sp + max(1, round(float(self.S))))
        if edge == "top":
            dx, dy = 0.0, work.top + guard - y
        elif edge == "left":
            dx, dy = work.left + guard - x, 0.0
        elif edge == "right":
            dx, dy = work.right - guard - (x + width), 0.0
        else:
            return False
        if abs(dx) <= .01 and abs(dy) <= .01:
            return False
        self.pos[0] += dx
        self.pos[1] += dy
        self.frame_dirty = True
        return True

    def gaming_edge_dock_target(self):
        """Resolve the intended drop target from the active strip, not the host buffer."""
        if not self._small_gaming_strip_active():
            return None, None
        rect = self._panel_screen_rect()
        if rect is None:
            return None, None
        x, y, width, height = rect
        work, _ = work_area_at(round(x + width / 2), round(y + height / 2))
        threshold = max(20.0, GAMING_EDGE_DOCK_DIP * max(1.0, float(self.S)))
        return gaming_dock_edge(rect, work, threshold), work

    def maintain_gaming_edge_dock(self):
        """Hold an edge-docked strip in frame while its size springs settle."""
        edge = getattr(self, "gaming_dock_edge", None)
        if (edge not in ("top", "left", "right") or
                not self._small_gaming_strip_active() or getattr(self, "drag", None)):
            return False
        rect = self._panel_screen_rect()
        if rect is None:
            return False
        x, y, width, height = rect
        work, _ = work_area_at(round(x + width / 2), round(y + height / 2))
        if edge in ("left", "right") and self.panel_side != edge:
            self.set_panel_side(edge, preserve=True, work=work)
        moved = self._snap_gaming_strip_to_edge(edge, work)
        return self.keep_panel_in_work_area(work=work) or moved

    def settle_gaming_edge_dock(self):
        """Turn a deliberate top/side drop into the matching strip orientation."""
        edge, work = self.gaming_edge_dock_target()
        if edge is None:
            self.gaming_dock_edge = None
            return False
        target = "horizontal" if edge == "top" else "vertical"
        if edge in ("left", "right"):
            self.set_panel_side(edge, preserve=True, work=work)
        changed = self.set_gaming_view(target, dock_edge=edge)
        self.gaming_dock_edge = edge
        self._snap_gaming_strip_to_edge(edge, work)
        self.keep_panel_in_work_area(work=work)
        if changed:
            self.old_page_springs()
            self.draw_content(crossfade=True)
        self.frame_dirty = True
        return True

    def set_gaming_view(self, view, dock_edge=None):
        """Select Horizontal, Vertical or Bubble without changing strip controls."""
        view = normalize_gaming_view(view)
        self.gaming_dock_edge = dock_edge if dock_edge in ("top", "left", "right") else None
        if view not in dict(GAMING_VIEWS) or view == self.panel.gaming_view:
            return False

        old = self.panel.gaming_view
        on_gaming = self.panel.page == "gaming"
        width = height = morph = None
        if on_gaming:
            width = self.springs.get("width", self.panel.w, k=240, zeta=0.90)
            height = self.springs.get("height", self.panel.height(self.snap), k=320, zeta=0.86)
            morph = self.springs.get(
                "hardware_morph", 1.0 if old == "bubble" else 0.0,
                k=220, zeta=0.86)
            if view == "bubble" and old != "bubble":
                # Measure the active strip, not the Settings page.
                self.gaming_top = self.gaming_anchor()

        if view == "bubble" and old in ("horizontal", "vertical"):
            self.panel.gaming_restore_view = old
        elif view in ("horizontal", "vertical"):
            self.panel.gaming_restore_view = view

        self.panel.gaming_view = view
        self.panel.tabs_open = False
        self.panel.tabs_t = 0.0
        self.springs.pop("tabs", None)
        self.panel.update_width()

        cfg = engine.load_config()
        cfg["gaming_view"] = view
        cfg["gaming_restore_view"] = self.panel.gaming_restore_view
        engine.save_config(cfg)

        if on_gaming:
            for spring in (width, height, morph):
                spring.k = 235.0
                spring.c = 2 * 0.86 * spring.k ** 0.5
            width.target = self.panel.w
            height.target = self.panel.height(self.snap)
            morph.target = 1.0 if view == "bubble" else 0.0
        return True

    def begin_panel_drag(self):
        """Begin a free panel drag with a monitor handoff that cannot oscillate."""
        cx, cy = cursor_pos()
        self.drag = (cx - self.pos[0], cy - self.pos[1])
        self.drag_click = None
        self.drag_origin = (cx, cy)
        self.drag_moved = False
        self.drag_work = self._panel_work_area() if hasattr(self, "glass") else None
        user32.SetCapture(self.hwnd)

    def move_dragged_panel(self, x, y):
        """Move one frame of a direct drag, preserving the cursor across monitors."""
        if self.drag is None or self.pos is None:
            return False
        nx, ny = x - self.drag[0], y - self.drag[1]
        if abs(nx - self.pos[0]) + abs(ny - self.pos[1]) <= 0:
            return False
        if not self.drag_moved:
            self.drag_moved = True
            # A free drag deliberately detaches the strip from a prior edge.
            self.gaming_dock_edge = None
        self.vel += (nx - self.pos[0], ny - self.pos[1])
        self.pos = [nx, ny]
        self.pinned = True
        # The renderer is present for every live Blob window. Keeping this
        # fallback makes a partially initialized host safely movable instead
        # of turning an input event into a failed drag.
        if not hasattr(self, "glass"):
            return True
        work = self._drag_work_for_cursor(x, y)
        self.maybe_flip_panel_side((x, y), work)
        self.keep_panel_in_work_area((x, y), work)
        return True

    def finish_panel_drag(self):
        """Finalize a moved drag and adopt the relevant Gaming edge if any."""
        moved = bool(self.drag_moved)
        self.drag_work = None
        if moved:
            self.settle_gaming_edge_dock()

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
        width = self.springs.get("width", self.panel.w, k=240, zeta=.90)
        height = self.springs.get("height", self.panel.height(self.snap), k=320, zeta=.86)
        morph = self.springs.get(
            "music_morph", 1.0 if self.panel.music_view == "bubble" else 0.0,
            k=220, zeta=.86)
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
        self._sync_music_ambient()
        if view == "bubble" or morph.x > .001:
            for spring in (width, height, morph):
                spring.k = 235.0
                spring.c = 2 * .86 * spring.k ** .5
        width.target = self.panel.w
        height.target = self.panel.height(self.snap)
        morph.target = 1.0 if view == "bubble" else 0.0

    def begin_bubble_drag(self, key):
        """Begin a deferred click-or-drag from a MiniBlob body or satellite."""
        cx, cy = cursor_pos()
        self.drag = (cx - self.pos[0], cy - self.pos[1])
        self.drag_click = key
        self.drag_origin = (cx, cy)
        self.drag_moved = False
        self.drag_work = self._panel_work_area() if hasattr(self, "glass") else None
        user32.SetCapture(self.hwnd)
        # Music retains tap/double-tap/hold playback behavior unless the
        # pointer actually moves beyond the drag threshold.
        if key == "mbubble:gesture":
            self.start_music_bubble_press()

    def cancel_music_bubble_press(self):
        """Stop a pending music gesture without releasing an active drag."""
        if not self.music_press_active:
            return
        self.music_press_active = self.music_hold_fired = False
        user32.KillTimer(self.hwnd, 5)

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
            self.music_click_pending = False
            self.music_click_deadline = 0.0
            user32.KillTimer(self.hwnd, 4)
            return True
        now = time.perf_counter()
        if (self.music_click_pending and
                now <= getattr(self, "music_click_deadline", 0.0)):
            self.music_click_pending = False
            self.music_click_deadline = 0.0
            user32.KillTimer(self.hwnd, 4)
            self.media.next()
        else:
            # A normal tap must feel like a real play/pause button. The old
            # implementation waited for the double-tap window before toggling,
            # which made the bubble appear dead and was especially noticeable
            # when Apple Music was already active. The second tap still becomes
            # Next, but the first tap is now dispatched immediately.
            self.music_click_pending = False
            self.music_click_deadline = now + 0.32
            self.media.toggle()
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
        if getattr(self.panel, "magnifier_active", False):
            self._sync_tool_capture(force=True)
        if on:
            self.refresh_backdrop()
        self.frame_dirty = True

    def refresh_backdrop(self):
        """Grab one clean frame of what's behind the panel while it's briefly hidden."""
        if not self.captureable or getattr(self.panel, "magnifier_active", False):
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
        if getattr(self.panel, "tools_open", False):
            return False
        return ((self.panel.page == "blob" and self.panel.hardware_view == "bubble") or
                (self.panel.page == "music" and self.panel.music_view == "bubble") or
                (self.panel.page == "sound" and self.panel.sound_view == "bubble") or
                (self.panel.page == "gaming" and getattr(self.panel, "gaming_view", "strip") == "bubble") or
                (self.panel.page == "settings" and getattr(self.panel, "settings_view", "card") == "bubble"))

    def bubble_proximity_target(self):
        """Return cursor proximity to the active bubble in supersampled panel space."""
        now = time.perf_counter()
        if not (self.visible and self.bubble_mode and self.overlay_unlocked):
            self.bubble_linger_until = 0.0
            return 0.0
        # Resolve against the rendered canvas each frame. Cached local positions
        # jump when the 104-DIP bubble grows a 240-DIP transparent palette canvas.
        rect = self._panel_screen_rect()
        if rect is not None:
            px, py = cursor_pos()
            x, y = (px - rect[0]) * self.ss, (py - rect[1]) * self.ss
            self.mouse_xy = (x, y)
        else:
            x, y = self.mouse_xy or (-1e6, -1e6)
        if rect is not None or self.mouse_in:
            S = self.panel.S
            hit = self.panel.hit(x, y)
            self.hover = hit
            if hit and (hit.startswith("tool:") or hit in BUBBLE_HOVER_KEYS):
                self.bubble_linger_until = now + BUBBLE_LINGER_SECONDS
                return 1.0
            lobes = (
                (self.panel.bubble_x(43), 55 * S, 39 * S),
                (self.panel.bubble_x(82.5), 22.5 * S, 21.5 * S),
            )
            gap = min(math.hypot(x - cx, y - cy) - radius for cx, cy, radius in lobes)
            approach = 34 * S
            proximity = max(0.0, min(1.0, (approach - gap) / approach))
            if proximity > 0.0:
                self.bubble_linger_until = now + BUBBLE_LINGER_SECONDS
                # A latched reveal avoids breathing/shrinking between buttons.
                return 1.0
            if self.panel.tool_reveal > .06:
                for cx, cy, orbit, radius, angle, half in self.panel.tool_arcs.values():
                    theta = math.atan2(y-cy, x-cx) - angle
                    theta = math.atan2(math.sin(theta), math.cos(theta))
                    nearest = angle + max(-half, min(half, theta))
                    if math.hypot(x-cx-orbit*math.cos(nearest),
                                  y-cy-orbit*math.sin(nearest)) < radius + 10*S:
                        self.bubble_linger_until = now + BUBBLE_LINGER_SECONDS
                        return 1.0
        if now < self.bubble_linger_until:
            return 1.0
        self.bubble_linger_until = 0.0
        return 0.0

    @property
    def bubble_drag_active(self):
        targets = self.bubble_drag_targets
        return self.hover in targets or self.drag_click in targets

    @property
    def bubble_drag_targets(self):
        """Only the MiniBlob body and its return satellite may reposition it."""
        if not (self.visible and self.bubble_mode and self.overlay_unlocked):
            return frozenset()
        return BUBBLE_DRAG_TARGETS.get(self.panel.page, frozenset())

    def bubble_drag_key(self, key):
        return bool(key and key in self.bubble_drag_targets)

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
        held = lambda vk: bool(user32.GetAsyncKeyState(vk) & 0x8000)
        # The same physical chord is used for the temporary drag override.
        # Checking both sides avoids the old generic Ctrl state being lost by
        # games that consume or remap one of the modifier messages.
        return held(LEFT_CONTROL) and held(RIGHT_CONTROL)

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
            self.drag_click = self.drag_origin = self.drag_work = None
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
        self.music_click_deadline = 0.0
        self.gaming_modifier_drag = False
        self.dual_control_down = {LEFT_CONTROL: False, RIGHT_CONTROL: False}
        self.dual_control_latched = False
        self.apply_gaming_input()
        self.draw_content()
        h = self.springs["height"]
        h.x = h.target
        if not self.pinned or self.pos is None:
            cx, cy = cursor_pos()
            work, _ = work_area_at(cx, cy)
            side = "left" if cx <= (work.left + work.right) / 2 else "right"
            self.panel_side = side
            self.panel.anchor_side = side
            self.glass.set_panel_side(side)
            # Park the fixed buffer against the chosen edge. panel_x() then
            # places the current panel there, whether it is a card or bubble.
            self.pos = [work.left if side == "left" else work.right - self.glass.W,
                        work.bottom - self.glass.H]
        self.keep_panel_in_work_area()
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
        if getattr(self, "_magnifier_follow", False):
            self.close_tool()
        self.gaming_modifier_drag = False
        self.dual_control_down = {LEFT_CONTROL: False, RIGHT_CONTROL: False}
        self.dual_control_latched = False
        self.hotkey_editing = self.panel.hotkey_editing = False
        self.music_press_active = self.music_hold_fired = self.music_click_pending = False
        self.music_click_deadline = 0.0
        self.drag_work = None
        user32.KillTimer(self.hwnd, 4)
        user32.KillTimer(self.hwnd, 5)
        self.apply_gaming_input()
        self.gaming.set_active(False)
        self.mouse_in = False
        self.hover = None
        self.bubble_linger_until = 0.0
        proximity = self.springs.get("bubble_proximity", 0.0)
        proximity.x = proximity.v = proximity.target = 0.0
        user32.KillTimer(self.hwnd, 1)
        self.interval = 0
        user32.ShowWindow(self.hwnd, 0)
        self.visible = False
        self.last_hide = time.time()

    def _schedule(self, animating=True):
        # One stable 60 FPS timer is enough for every authored motion. Event
        # handlers still request an immediate frame, while the timer avoids
        # overdriving the CPU at 125 Hz on the regular pages.
        want = 16
        if want != self.interval:
            self.interval = want
            user32.SetTimer(self.hwnd, 1, want, None)

    def _sync_tabs_texture(self, now, tabs):
        """Coalesce full texture rerasterization while the tab spring is moving."""
        if abs(tabs.x - self.panel.tabs_t) <= .004:
            return False
        last = getattr(self, "_tabs_texture_at", 0.0)
        if tabs.moving and now - last < TAB_TEXTURE_SECONDS:
            return False
        self.panel.tabs_t = tabs.x
        self._tabs_texture_at = now
        return True

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
            elif kind == "arc":
                _, key, geometry = c
                L = dict(geometry)
                hover = sp.get(prefix + "arc:hover:" + key, 0.0, k=420, zeta=.92)
                press = sp.get(prefix + "arc:press:" + key, 0.0, k=450, zeta=.8)
                hover.target = float(not prefix and self.hover == key)
                press.target = float(not prefix and self.pressed == key)
                # The palette uses the same pull-and-settle language as the
                # bottom menu: a hovered lobe leans toward the pointer and a
                # press briefly pushes it back. Rebuild its arc bounds from
                # the spring so shader shape, hit testing and glass stay one
                # object instead of stacking a circular hover lens on top.
                feedback = max(0.0, min(1.0, hover.x + .8 * press.x))
                base_rect = geometry["rect"]
                centre = ((base_rect[0] + base_rect[2]) / 2,
                          (base_rect[1] + base_rect[3]) / 2)
                base_radius = geometry.get("r", 1.0)
                base_orbit = (base_rect[2] - base_rect[0]) / 2 - base_radius
                orbit = base_orbit + 6 * S * hover.x + 5 * S * press.x
                radius = base_radius * (1.0 + .04 * hover.x + .06 * press.x)
                base_angle, base_half = geometry.get("arc", (0.0, 0.0))
                angle = base_angle + (0.045 * hover.x - 0.035 * press.x) * (1 if self.panel.anchor_side == "right" else -1)
                half = base_half * (1.0 + .04 * hover.x + .06 * press.x)
                outer = orbit + radius
                L["rect"] = (centre[0] - outer, centre[1] - outer,
                              centre[0] + outer, centre[1] + outer)
                L["r"] = radius
                L["arc"] = (angle, half)
                L["strength"] *= 1 + .18 * feedback + .16 * press.x
                L["lift"] += .08 * feedback - .04 * press.x
                if not prefix and key in getattr(self.panel, "tool_arcs", {}):
                    self.panel.tool_arcs[key] = (centre[0], centre[1], orbit, radius, angle, half)
                    # Keep the hit envelope in lockstep with the spring-driven
                    # lobe.  This prevents the pointer from feeling detached
                    # from a satellite while it is stretching toward it.
                    arc_angles = [angle - half, angle + half, angle]
                    for cardinal in (0, math.pi / 2, math.pi, -math.pi / 2):
                        delta = math.atan2(math.sin(cardinal - angle),
                                           math.cos(cardinal - angle))
                        if abs(delta) <= half:
                            arc_angles.append(cardinal)
                    xs = [centre[0] + orbit * math.cos(a) for a in arc_angles]
                    ys = [centre[1] + orbit * math.sin(a) for a in arc_angles]
                    self.panel.rects[key] = (min(xs) - radius, min(ys) - radius,
                                             max(xs) + radius, max(ys) + radius)
                self._fade(L, presence)
                out.append(L)
            elif kind == "hover":
                _, key, rect, r = c
                if key in BUBBLE_HOVER_KEYS:
                    # Bubble proximity already provides the single glossy
                    # surface.  Do not stack the generic frosted hover lens
                    # on top of it; the rectangle remains a live hit target.
                    continue
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
        self._sync_tool_capture()
        if getattr(self, "tools", None) and self.tools.poll_rates():
            self.draw_content()
        if (getattr(self, "clipboard", None) and self.panel.tools_open and
                self.panel.tool_view == "clipboard" and now >= self._clipboard_poll_at):
            self._clipboard_poll_at = now + .12
            if self.clipboard.poll():
                self.draw_content()
        if self._update_tabs_side():
            # A drag can cross the monitor midpoint without changing any
            # sensor state; redraw immediately so the menu/button follows it.
            self.draw_content()
        self.update_gaming_modifier_drag()
        dt = min(0.05, now - self.last_frame)
        self.last_frame = now
        self.springs.step(dt)
        magnifier_zoom = self.springs.get("magnifier_zoom", 1.0, k=190, zeta=.95)
        magnifier_zoom.target = self.panel.magnifier_zoom if self.panel.magnifier_active else 1.0
        fade = self.springs.get("fade", 1.0).x
        width = self.springs.get("width", self.panel.w, k=240, zeta=0.90)
        width.target = self.panel.w
        morph = self.springs.get(
            "hardware_morph",
            1.0 if (not self.panel.tools_open and
                    (self.panel.hardware_view == "bubble" or
                     (self.panel.page == "gaming" and self.panel.gaming_view == "bubble"))) else 0.0,
            k=220, zeta=0.86)
        morph.target = 1.0 if ((self.panel.page == "blob" and self.panel.hardware_view == "bubble") or
                              (self.panel.page == "gaming" and self.panel.gaming_view == "bubble")) \
            and not self.panel.tools_open else 0.0
        music_morph = self.springs.get(
            "music_morph", 1.0 if self.panel.music_view == "bubble" else 0.0,
            k=220, zeta=.86)
        music_morph.target = 1.0 if self.panel.page == "music" and self.panel.music_view == "bubble" \
            and not self.panel.tools_open else 0.0
        sound_morph = self.springs.get(
            "sound_morph", 1.0 if (self.panel.page == "sound" and
                                     self.panel.sound_view == "bubble") else 0.0,
            k=220, zeta=.86)
        sound_morph.target = 1.0 if (self.panel.page == "sound" and
                                     self.panel.sound_view == "bubble" and
                                     not self.panel.tools_open) else 0.0
        settings_morph = self.springs.get(
            "settings_morph", 1.0 if (self.panel.page == "settings" and
                                        self.panel.settings_view == "bubble") else 0.0,
            k=220, zeta=.86)
        settings_morph.target = 1.0 if (self.panel.page == "settings" and
                                        self.panel.settings_view == "bubble" and
                                        not self.panel.tools_open) else 0.0
        bubble_proximity = self.springs.get("bubble_proximity", 0.0,
                                            k=BUBBLE_PROXIMITY_K,
                                            zeta=BUBBLE_PROXIMITY_ZETA)
        bubble_proximity.target = self.bubble_proximity_target()
        reveal = max(0.0, min(1.0, bubble_proximity.x)) if self.bubble_mode else 0.0
        if abs(reveal - self.panel.tool_reveal) > 0.012:
            palette_available = bool(getattr(self.panel, "tool_palette_available", lambda: False)())
            was_palette = palette_available and self.panel.tool_reveal > .06
            self.panel.tool_reveal = reveal
            self.panel.update_width()
            is_palette = palette_available and self.panel.tool_reveal > .06
            if was_palette != is_palette and self.bubble_mode:
                # The canvas itself changes only at the quiet threshold. The
                # satellites provide the authored spring motion, so the base
                # bubble never stretches or slides during the handoff.
                width.x = width.target = self.panel.w
                width.v = 0.0
                height = self.springs.get("height", self.panel.height(self.snap), k=320, zeta=.86)
                height.x = height.target = self.panel.height(self.snap)
                height.v = 0.0
            self.draw_content()
        bass = self.springs.get("music_bass", 0.0, k=150, zeta=.72)
        mid = self.springs.get("music_mid", 0.0, k=210, zeta=.78)
        treble = self.springs.get("music_treble", 0.0, k=280, zeta=.82)
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
        if self._sync_tabs_texture(now, tabs):
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
                    self._sync_music_ambient()
                self.am_seen = self.am.version
                self.last_text = now
                self.draw_content()
        elif self.panel.page == "gaming" and now - self.last_text >= .5:
            self.last_text = now
            self.draw_content()
        viz_live = reactive_live or (self.panel.page == "sound" and
                    (self.sound.enabled or self.sound.spectrum.max() > 0.01))
        backdrop_live = bool(self.panel.page == "music" and self.panel.backdrop and
                             not self.panel.tools_open and
                             self.panel.music_view not in ("art", "bubble") and
                             getattr(self.media, "art", None) is not None)
        animating = self.springs.moving or self.drag is not None or bool(self.slider_drag)             or abs(self.vel).max() > 0.5
        # Poll the desktop on its own cadence. Animation can render the cached
        # glass scene every frame instead of forcing a full capture/upload.
        panel_h = self.springs["height"].x / self.ss
        if self.panel.tools_open and self.panel.tool_view in ("calculator", "clipboard", "blank"):
            self._constrain_calculator_frame()
            panel_h = self.springs["height"].x / self.ss
        # Size springs are allowed to overshoot for liquid motion, but never
        # beyond the monitor's work area. The same pass recovers a panel that
        # was left half outside a display by an interrupted drag or a layout
        # change, before capture and layered-window presentation.
        if self.drag is None and not getattr(self, "_magnifier_follow", False):
            if not self.maintain_gaming_edge_dock():
                self.keep_panel_in_work_area()
        content_dirty = self.frame_dirty
        panel_y = self._panel_y(panel_h)
        if hasattr(self.glass, "set_tool_card"):
            self.glass.set_tool_card(self.panel.tools_open and self.panel.tool_view in ("calculator", "clipboard", "blank"))
        self._track_magnifier(width.x / self.ss, panel_h, panel_y)
        if getattr(self, "_magnifier_follow", False):
            # WM_SETCURSOR is not guaranteed while a layered window owns
            # capture, so reinforce the invisible cursor on every frame too.
            user32.SetCursor(getattr(self, "cur_blank", None))
        if hasattr(self.glass, "set_magnifier"):
            if self.panel.magnifier_active:
                aperture = list(v / self.ss for v in self.panel.magnifier_aperture())
                offset = panel_h - self.panel.height(self.snap) / self.ss
                aperture[1] += offset
                aperture[3] += offset
                self.glass.set_magnifier(aperture, magnifier_zoom.x)
            else:
                self.glass.set_magnifier(None)
        # A UI/content redraw does not require a fresh desktop grab.  Let the
        # renderer's 60 FPS capture budget protect drag and hover smoothness.
        background_changed = self.glass.capture(self.pos[0], self.pos[1], width.x / self.ss,
                                                panel_h, force=False, panel_y=panel_y)
        if not (background_changed or content_dirty or animating or viz_live or backdrop_live):
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
        panel_shape = 0.0 if self.panel.tools_open else max(morph.x, music_morph.x,
                                                               sound_morph.x, settings_morph.x)
        self.glass.set_panel_shape(panel_shape)
        if hasattr(self.glass, "set_bubble_proximity"):
            self.glass.set_bubble_proximity(bubble_proximity.x)
        if hasattr(self.glass, "set_tool_expansion"):
            palette_open = (self.panel.tool_reveal > .06 and
                            bool(getattr(self.panel, "tool_palette_available", lambda: False)()))
            self.glass.set_tool_expansion(1.0 if palette_open else 0.0)
        if hasattr(self.glass, "set_bubble_time"):
            self.glass.set_bubble_time(now)
        if self.panel.page == "music":
            self.glass.set_bubble_audio(bass.x, mid.x, treble.x)
        else:
            self.glass.set_bubble_audio(0.0, 0.0, 0.0)
        if hasattr(self.glass, "set_backdrop_motion"):
            if backdrop_live:
                bt = now - self.backdrop_started
                self.glass.set_backdrop_motion(
                    1.075 + 0.018 * math.sin(bt * 0.11),
                    0.020 * math.sin(bt * 0.17),
                    0.016 * math.cos(bt * 0.13),
                    0.008 * math.sin(bt * 0.09), bt, 0.78)
            else:
                self.glass.set_backdrop_motion(alpha=0.0)
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
        if self.panel.magnifier_active:
            # Clear the animated Blob pointer immediately while the native
            # cursor is being hidden for the live lens.
            self.attached = self.detaching = None
            self.glass.set_pointer(None, 0.0, -1, 0.0)
            return
        style = "system" if self.bubble_mode or self.panel.magnifier_active else self.pointer_style
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
        if getattr(self.panel, "tools_open", False):
            tool_top = getattr(self, "tool_top", None)
            return tool_top if tool_top is not None else self.glass.panel_y(int(round(height)))
        morph = self.springs.get("hardware_morph", 0.0)
        if self.panel.page == "blob" and (self.panel.hardware_view == "bubble" or morph.x > .001):
            return self.hardware_top
        if self.panel.page == "gaming" and (self.panel.gaming_view == "bubble" or morph.x > .001):
            return getattr(self, "gaming_top", self.glass.panel_y(int(round(height))))
        music_morph = self.springs.get("music_morph", 0.0)
        if self.panel.page == "music" and (self.panel.music_view == "bubble" or music_morph.x > .001):
            return self.music_top
        sound_morph = self.springs.get("sound_morph", 0.0)
        if self.panel.page == "sound" and (self.panel.sound_view == "bubble" or sound_morph.x > .001):
            return self.sound_top
        settings_morph = self.springs.get("settings_morph", 0.0)
        if self.panel.page == "settings" and (self.panel.settings_view == "bubble" or
                                                settings_morph.x > .001):
            return self.settings_top
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

    def open_calculator_tool(self, new=False, mode=None):
        """Grow a calculator from the current bubble without moving its anchor."""
        if not getattr(self, "tools", None):
            return
        if not self.panel.tools_open:
            # Capture the bubble's current top edge before its dimensions change.
            self.tool_top = self._panel_y(self.panel.height(self.snap))
        if new:
            self.tools.new_calculator()
        if mode is not None:
            self.tools.set_mode(mode)
        self.panel.calculator_mode = self.tools.mode
        self.panel.calculator_menu = None
        self.panel.calculator_history = False
        self.panel.tool_view = "calculator"
        self.panel.tool_minimized = False
        self.panel.tool_reveal = 0.0
        self.panel.tools_open = True
        self.panel.update_width()
        self.fit_calculator_in_work_area()
        self.pinned = True
        user32.SetForegroundWindow(self.hwnd)
        self._sync_tool_capture()
        self.frame_dirty = True

    def fit_calculator_in_work_area(self, screen_hint=None, work=None):
        if not (getattr(self.panel, "tools_open", False) and
                self.panel.tool_view in ("calculator", "clipboard", "blank")):
            return False
        rect = self._panel_screen_rect()
        if rect is None:
            return False
        x, y, w, h = rect
        point = screen_hint or (x+w/2, y+h/2)
        if work is None:
            work, _ = work_area_at(round(point[0]), round(point[1]))
        self._calculator_work = work
        margin = self.glass.sp
        available_h = min(work.bottom-work.top-2*margin, self.glass.H-2*margin)
        available_w = min(work.right-work.left-2*margin, self.glass.W-2*margin)
        scale = max(.1, min(1.0, available_h/(self.panel.tool_card_height()*self.S),
                           available_w/(self.panel.TOOL_W*self.S)))
        changed = abs(scale-self.panel.tool_size_scale) > .001
        self.panel.tool_size_scale = scale
        self.panel.update_width()
        target_h = self.panel.height(self.snap)/self.ss
        self.tool_top = max(margin, min(self.tool_top if self.tool_top is not None else margin,
                                       self.glass.H-margin-target_h))
        # Clamp the destination before the expansion starts, including the shadow.
        target_w = self.panel.w/self.ss
        px, py = self.glass.panel_x(target_w), self.tool_top
        self.pos[0] = max(work.left+margin-px, min(self.pos[0], work.right-margin-px-target_w))
        self.pos[1] = max(work.top+margin-py, min(self.pos[1], work.bottom-margin-py-target_h))
        return changed

    def _constrain_calculator_frame(self):
        work = getattr(self, "_calculator_work", None)
        if work is None:
            return
        margin = self.glass.sp
        width, height = self.springs["width"], self.springs["height"]
        for spring, limit in ((width, min(work.right-work.left, self.glass.W)-2*margin),
                              (height, min(work.bottom-work.top, self.glass.H)-2*margin)):
            if spring.x > limit*self.ss:
                spring.x, spring.v = limit*self.ss, 0
        w, h = width.x/self.ss, height.x/self.ss
        self.tool_top = max(margin, min(self.tool_top, self.glass.H-margin-h))
        px, py = self.glass.panel_x(w), self.tool_top
        self.pos[0] = max(work.left+margin-px, min(self.pos[0], work.right-margin-px-w))
        self.pos[1] = max(work.top+margin-py, min(self.pos[1], work.bottom-margin-py-h))

    def open_utility_tool(self, view):
        """Open a sibling tool surface from the same animated tool layer."""
        if not getattr(self, "tools", None):
            return
        if view == "clipboard" and getattr(self, "clipboard", None):
            self.clipboard.poll(force=True)
        if view == "magnifier" and not getattr(self, "_magnifier_follow", False):
            self._magnifier_home = (list(self.pos), self.pinned)
        if not self.panel.tools_open:
            self.tool_top = self._panel_y(self.panel.height(self.snap))
        self.panel.tool_view = view if view in ("magnifier", "clipboard", "blank") else "calculator"
        self.panel.tool_minimized = False
        self.panel.tool_reveal = 0.0
        self.panel.tools_open = True
        self.panel.update_width()
        if view in ("clipboard", "blank"):
            self.fit_calculator_in_work_area()
            self.pinned = True
        if view == "magnifier":
            self.tool_top = max(self.glass.sp, min(self.tool_top,
                self.glass.H - self.glass.sp - self.panel.height(self.snap) / self.ss))
            self.springs.get("magnifier_zoom", 1.0, k=190, zeta=.95).target = self.panel.magnifier_zoom
            self._magnifier_follow = True
            self.pinned = True
            self.drag = self.drag_click = self.drag_origin = None
            user32.SetForegroundWindow(self.hwnd)
            user32.SetCapture(self.hwnd)
            user32.SetCursor(getattr(self, "cur_blank", None))
        self._sync_tool_capture()
        self.frame_dirty = True

    def _sync_tool_capture(self, force=False):
        """The live lens temporarily excludes itself from capture; preserve settings."""
        active = not getattr(self, "full", False) and getattr(self.panel, "magnifier_active", False)
        if not active:
            self._end_magnifier_follow()
        if not force and active == getattr(self, "_magnifier_live", False):
            return
        self._magnifier_live = active
        user32.SetWindowDisplayAffinity(self.hwnd, 0x11 if active or not self.captureable else 0)
        self.glass.source.frozen = bool(self.captureable and not active)
        self.glass._last_key = None
        if not active:
            self.glass.set_magnifier(None)
            if self.captureable:
                self.refresh_backdrop()
        self.frame_dirty = True

    def _track_magnifier(self, width, height, top):
        if not getattr(self, "_magnifier_follow", False):
            return
        aperture = self.panel.magnifier_aperture()
        cx = (aperture[0] + aperture[2]) / (2*self.ss)
        cy = (aperture[1] + aperture[3]) / (2*self.ss)
        cy += height - self.panel.height(self.snap) / self.ss
        x, y = cursor_pos()
        position = [round(x-self.glass.panel_x(width)-cx), round(y-top-cy)]
        if position != self.pos:
            self.pos = position
            # The magnifier is a free reading lens: it may cross monitor
            # edges and follow the pointer without the normal panel clamp.
            self.frame_dirty = True

    def _end_magnifier_follow(self):
        if not getattr(self, "_magnifier_follow", False):
            return
        self._magnifier_follow = False
        if user32.GetCapture() == self.hwnd:
            user32.ReleaseCapture()
        home = getattr(self, "_magnifier_home", None)
        if home is not None:
            self.pos, self.pinned = home
            self._magnifier_home = None
        user32.SetCursor(getattr(self, "cur_arrow", None))
        self.frame_dirty = True

    def adjust_magnifier_zoom(self, amount=None):
        self.panel.magnifier_zoom = (2.0 if amount is None else
                                     max(0.25, min(40.0, self.panel.magnifier_zoom + amount)))
        self.frame_dirty = True

    def minimize_tool(self, local_x=None):
        """Return to the bubble at the same monitor-facing header rail."""
        self.prepare_minimize_anchor(local_x)
        self._keep_tool_return_anchor()
        self.panel.tools_open = False
        self.panel.tool_minimized = True
        self.panel.tool_reveal = 1.0
        self.panel.update_width()
        self._sync_tool_capture()
        self.frame_dirty = True

    def close_tool(self, local_x=None):
        self.prepare_minimize_anchor(local_x)
        self._keep_tool_return_anchor()
        self.panel.tools_open = False
        self.panel.tool_minimized = False
        self.panel.tool_view = None
        self.panel.tool_reveal = 0.0
        self.panel.update_width()
        self._sync_tool_capture()
        self.frame_dirty = True

    def _keep_tool_return_anchor(self):
        if self.panel.tool_view not in ("calculator", "clipboard", "blank") or self.tool_top is None:
            return
        # Retraction shares the calculator's top edge. Restoring an old page's
        # bottom anchor here would send the shrinking card below the monitor.
        anchor = {"blob": "hardware_top", "music": "music_top", "sound": "sound_top",
                  "gaming": "gaming_top", "settings": "settings_top"}.get(self.panel.page)
        if anchor:
            setattr(self, anchor, self.tool_top)

    def click(self, h, x):
        kind, _, key = h.partition(":")
        crossfade = False
        if kind == "page":
            if key != self.panel.page:
                if key != "gaming":
                    self.gaming_dock_edge = None
                self.media_seen = None
                self.old_page_springs()
                self.panel.page = key
                self.panel.music_menu = None
                self.panel.sound_menu = False
                self._sync_music_ambient()
                self.apply_gaming_input()
                self.panel.update_width()
                if key == "gaming":
                    # A new Gaming session starts as the clean FPS bubble.
                    # Its restore satellite appears only after a fresh hover;
                    # no palette geometry may survive a page transition.
                    self.panel.tool_reveal = 0.0
                    self.bubble_linger_until = 0.0
                    bubble = self.springs.get("bubble_proximity")
                    if hasattr(bubble, "x"):
                        bubble.x = bubble.v = bubble.target = 0.0
                    self.panel.tabs_open = False
                    self.panel.tabs_t = 0
                    self.springs.pop("tabs", None)
                    if getattr(self.panel, "gaming_view", "horizontal") == "bubble":
                        self.gaming_top = self.gaming_anchor()
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
            if key == "bubble":
                self.prepare_minimize_anchor(x)
            self.set_hardware_view(key)
            crossfade = True
        elif kind == "sview":
            if key == "bubble":
                self.prepare_minimize_anchor(x)
            self.set_sound_view(key)
            crossfade = True
        elif kind == "settingsview":
            if key == "bubble":
                self.prepare_minimize_anchor(x)
            self.set_settings_view(key)
            crossfade = True
        elif kind == "preset":
            self.sound.set_preset(key)
        elif kind == "mview":
            if key == "bubble":
                self.prepare_minimize_anchor(x)
            self.set_music_view(key)
            self.panel.scroll = {}
            crossfade = True
            self.old_page_springs()
            if key == "queue":
                self.am.refresh_queue()
                self.last_queue = time.perf_counter()
        elif kind == "cover" and key == "art":
            self.set_music_view("art")
            self.panel.scroll = {}
            crossfade = True
            self.old_page_springs()
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
            if key == "bubble":
                self.prepare_minimize_anchor(x)
            view = self.panel.gaming_restore_view if key == "restore" else normalize_gaming_view(key)
            if self.set_gaming_view(view):
                crossfade = True
                self.old_page_springs()
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
            self.panel.sound_menu = not self.panel.sound_menu
            if self.panel.sound_menu:
                self.sound.refresh_devices()
        elif kind == "output":
            try:
                index = int(key)
            except ValueError:
                index = -1
            options = self.sound.output_options()
            if 0 <= index < len(options):
                self.sound.select_output(options[index])
            self.panel.sound_menu = False
        elif kind == "fxquit":
            self.sound.quit_fxsound()
        elif kind == "setupaudio":
            self.setup_audio_support()
        elif kind == "slider":
            self.slider_drag = key
            self.set_slider(key, self.panel.slider_value(key, x))
            user32.SetCapture(self.hwnd)
        elif kind == "tool":
            if key == "calculator":
                self.open_calculator_tool(new=True)
            elif key == "restore":
                if self.panel.tool_view in ("magnifier", "clipboard", "blank"):
                    self.open_utility_tool(self.panel.tool_view)
                else:
                    self.open_calculator_tool(new=False)
            elif key in ("blank", "currency"):
                # ``currency`` remains a harmless compatibility route for
                # old renders; the visible satellite is now the blank slot.
                self.open_utility_tool("blank")
            elif key in ("magnifier", "clipboard"):
                self.open_utility_tool(key)
            elif key == "minimize":
                self.minimize_tool(x)
            elif key == "close":
                self.close_tool(x)
            elif key == "new":
                self.open_calculator_tool(new=True)
        elif kind == "magnify":
            self.adjust_magnifier_zoom(.5 if key == "more" else -.5 if key == "less" else None)
        elif kind == "toolmode":
            if getattr(self, "tools", None):
                self.tools.set_mode(key)
                self.panel.calculator_mode = self.tools.mode
                self.panel.calculator_menu = None
                self.panel.calculator_history = False
                self.fit_calculator_in_work_area()
                crossfade = True
        elif kind == "calcmenu":
            self.panel.calculator_menu = None if key == "close" or self.panel.calculator_menu == key else key
            crossfade = True
        elif kind == "calcpanel":
            user32.SetForegroundWindow(self.hwnd)
            if key == "history":
                self.panel.calculator_history = not self.panel.calculator_history
                self.panel.calculator_history_page = 0
                self.panel.calculator_menu = None
                self.fit_calculator_in_work_area()
                crossfade = True
        elif kind == "calchistory":
            self.panel.calculator_history_page = max(0, self.panel.calculator_history_page + (1 if key == "next" else -1))
            crossfade = True
        elif kind == "calcnotecursor":
            lines = self.tools.notes.split("\n")
            row = max(0, min(len(lines)-1, int(key)))
            self.panel.calculator_notes_cursor = sum(len(line)+1 for line in lines[:row])+len(lines[row])
        elif kind == "calcrecall":
            self.tools.recall(int(key))
            self.panel.calculator_history = False
            self.fit_calculator_in_work_area()
            crossfade = True
        elif kind == "calcunit":
            part, value = key.split(":", 1)
            self.tools.select_conversion(part, value)
            self.panel.calculator_menu = None
            crossfade = True
        elif kind == "calcrate":
            self.tools.refresh_rates(force=True)
        elif kind == "cliptab":
            if getattr(self, "clipboard", None):
                self.clipboard.set_tab(key)
                crossfade = True
        elif kind == "clip":
            if getattr(self, "clipboard", None):
                action, _, ident = key.partition(":")
                if action == "refresh":
                    self.clipboard.poll(force=True)
                elif action == "select":
                    self.clipboard.select(ident)
                elif action == "mark":
                    self.clipboard.toggle_selection(ident)
                elif action == "copy":
                    self.clipboard.copy(ident)
                elif action == "copyselected":
                    self.clipboard.copy_selected()
                elif action == "pin":
                    self.clipboard.toggle_pin(ident)
                elif action == "clear":
                    self.clipboard.clear_unpinned()
                elif action == "prev":
                    self.clipboard.move_page(-1)
                elif action == "next":
                    self.clipboard.move_page(1)
        elif kind == "calc":
            if getattr(self, "tools", None):
                self.tools.press(key)
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
            if getattr(self, "_magnifier_follow", False):
                if msg == WM_SETCURSOR:
                    user32.SetCursor(getattr(self, "cur_blank", None))
                    return 1
                if msg == 0x205 or (msg == WM_CAPTURECHANGED and lp != hwnd):
                    self.click("tool:close", 0)
                    return 0
                if msg in (WM_LBUTTONDOWN, WM_LBUTTONUP, 0x204):
                    return 0
                if msg == WM_MOUSEMOVE:
                    self.frame_dirty = True
                    if time.perf_counter() - self.last_frame >= FRAME_PACING_SECONDS:
                        self.frame()
                    user32.SetCursor(getattr(self, "cur_blank", None))
                    return 0
            if msg == WM_DPICHANGED:
                self.sync_dpi(wp & 0xFFFF)
                return 0
            if msg in (WM_SETTINGCHANGE, WM_DISPLAYCHANGE):
                # Taskbar/work-area and monitor-layout changes can happen with
                # no mouse movement. Refit the actual rendered panel at once.
                if self.visible:
                    if not self.maintain_gaming_edge_dock():
                        self.keep_panel_in_work_area()
                    self.frame_dirty = True
                    self.frame()
                return 0
            if msg == WM_APP_GAMING_LOCK or (msg == WM_HOTKEY and wp == GAMING_HOTKEY):
                self.toggle_overlay_input()
                return 0
            if self.gaming_drag_active:
                if msg == 0x84:  # WM_NCHITTEST: shortcut modifiers temporarily enable dragging
                    return 1  # HTCLIENT
                if msg == WM_LBUTTONDOWN:
                    self.begin_panel_drag()
                    return 0
                if msg == WM_MOUSEMOVE:
                    if self.drag:
                        x, y = cursor_pos()
                        if self.move_dragged_panel(x, y):
                            if time.perf_counter() - self.last_frame >= FRAME_PACING_SECONDS:
                                self.frame()
                    return 0
                if msg in (WM_LBUTTONUP, WM_CAPTURECHANGED):
                    if self.drag:
                        self.finish_panel_drag()
                        self.drag = None
                        self.drag_click = self.drag_origin = None
                        self.drag_moved = False
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
                    self.music_click_deadline = 0.0
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
                if self.panel.tools_open and self.panel.tool_view in ("calculator", "clipboard", "blank"):
                    if self.panel.tool_view == "calculator" and self.panel.calculator_menu:
                        self.click("calcmenu:close", 0)
                    elif self.panel.tool_view == "calculator" and self.panel.calculator_history:
                        self.click("calcpanel:history", 0)
                    else:
                        self.click("tool:minimize", 0)
                    return 0
                if self.panel.magnifier_active:
                    self.click("tool:close", 0)
                    return 0
                if self.panel.page == "music" and self.panel.music_view != "now":
                    self.click("mview:now", 0)
                else:
                    self.hide()
                return 0
            if (msg == WM_KEYDOWN and self.panel.tools_open and self.panel.tool_view == "calculator"
                    and self.tools.mode == "notes" and not self.panel.calculator_menu and not self.panel.calculator_history
                    and wp in (0x25, 0x27, 0x24, 0x23, 0x2E)):
                text = self.tools.notes
                cursor = len(text) if self.panel.calculator_notes_cursor is None else self.panel.calculator_notes_cursor
                if wp == 0x25:
                    cursor = max(0, cursor-1)
                elif wp == 0x27:
                    cursor = min(len(text), cursor+1)
                elif wp == 0x24:
                    cursor = text.rfind("\n", 0, cursor)+1
                elif wp == 0x23:
                    end = text.find("\n", cursor)
                    cursor = len(text) if end < 0 else end
                elif wp == 0x2E:
                    self.tools.notes = text[:cursor]+text[cursor+1:]
                    self.tools.save()
                self.panel.calculator_notes_cursor = cursor
                self.draw_content()
                self.frame_dirty = True
                return 0
            if msg == 0x102 and self.panel.tools_open and self.panel.tool_view == "calculator":
                if self.panel.calculator_menu or self.panel.calculator_history:
                    return 0
                ch = chr(wp)
                if self.tools.mode == "notes":
                    text = self.tools.notes
                    cursor = len(text) if self.panel.calculator_notes_cursor is None else min(len(text), self.panel.calculator_notes_cursor)
                    if ch == "\b":
                        self.tools.notes = text[:max(0, cursor-1)]+text[cursor:]
                        cursor = max(0, cursor-1)
                    elif ch == "\r" or ch.isprintable():
                        insert = "\n" if ch == "\r" else ch
                        self.tools.notes = (text[:cursor]+insert+text[cursor:])[:16000]
                        cursor = min(len(self.tools.notes), cursor+1)
                    self.panel.calculator_notes_cursor = cursor
                    self.tools.save()
                    self.panel.calculator_notes_scroll = max(0, self.tools.notes[:cursor].count("\n")-9)
                else:
                    if ch in "\r=":
                        self.tools.press("=")
                    elif ch == "\b":
                        self.tools.press("DEL")
                    elif ch.isprintable() and len(self.tools.calculator.expression) < 2048:
                        self.tools.press(ch)
                self.draw_content()
                self.frame_dirty = True
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
                if self.panel.tools_open and self.panel.tool_view == "calculator":
                    step = -1 if ctypes.c_short(wp >> 16).value > 0 else 1
                    if self.panel.calculator_history:
                        self.panel.calculator_history_page = max(0, self.panel.calculator_history_page+step)
                    elif self.tools.mode == "notes":
                        self.panel.calculator_notes_scroll = max(0, min(max(0, len(self.tools.notes.split("\n"))-10), self.panel.calculator_notes_scroll+step))
                    self.draw_content()
                    self.frame_dirty = True
                    return 0
                if self.panel.tools_open and self.panel.tool_view == "clipboard":
                    step = -1 if ctypes.c_short(wp >> 16).value > 0 else 1
                    if getattr(self, "clipboard", None):
                        self.clipboard.move_page(step)
                    self.draw_content()
                    self.frame_dirty = True
                    return 0
                if self.panel.magnifier_active:
                    delta = ctypes.c_short(wp >> 16).value
                    accumulated = getattr(self, "_magnifier_wheel", 0) + delta
                    notches = math.trunc(accumulated / 120)
                    self._magnifier_wheel = accumulated - notches * 120
                    if notches:
                        self.adjust_magnifier_zoom(.5 * notches)
                    self.draw_content()
                    return 0
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
                        if self.drag_moved and self.drag_click == "mbubble:gesture":
                            # A real move wins over the music tap/hold gesture.
                            self.cancel_music_bubble_press()
                        if not self.drag_moved:
                            return 0
                    if self.move_dragged_panel(x, y):
                        if self.fit_calculator_in_work_area((x, y), getattr(self, "drag_work", None)):
                            self.draw_content()
                        if time.perf_counter() - self.last_frame >= FRAME_PACING_SECONDS:
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
                    if time.perf_counter() - self.last_frame >= FRAME_PACING_SECONDS:
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
                body_hit = getattr(self.panel, "bubble_body_hit", None)
                body = body_hit(x, y) if self.bubble_mode and body_hit else None
                if body and (not h or h.startswith("tool:")):
                    # Keep the primary lobe draggable through the width/height
                    # handoff used by the transient tool palette. Tool-lobe
                    # envelopes may overlap the body while they extrude, but
                    # the satellite/tool centers remain separate hit targets.
                    h = body
                if self.bubble_drag_key(h):
                    self.begin_bubble_drag(h)
                elif h:
                    self.pressed = h
                    self.click(h, x)
                elif self.panel.page == "music" and self.panel.music_menu:
                    self.panel.music_menu = None
                    self.draw_content()
                    self.frame_dirty = True
                elif self.panel.page == "sound" and self.panel.sound_menu:
                    self.panel.sound_menu = False
                    self.draw_content()
                    self.frame_dirty = True
                elif not self.bubble_mode:
                    self.begin_panel_drag()
                return 0
            if msg in (WM_LBUTTONUP, WM_CAPTURECHANGED):
                if self.music_press_active:
                    if msg == WM_LBUTTONUP and not self.drag_moved:
                        self.finish_music_bubble_press()
                    else:
                        self.cancel_music_bubble_press()
                        if user32.GetCapture() == hwnd:
                            user32.ReleaseCapture()
                    # Music gestures now share the same direct drag contract
                    # as every MiniBlob body. A completed tap is handled above;
                    # either way the deferred drag state cannot linger.
                    if self.drag:
                        self.finish_panel_drag()
                        self.drag = None
                        self.drag_click = self.drag_origin = None
                        self.drag_moved = False
                        if getattr(self, "captureable", False):
                            self.refresh_backdrop()
                    self._schedule(True)
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
                    self.finish_panel_drag()
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
                if getattr(self.panel, "magnifier_active", False):
                    user32.SetCursor(getattr(self, "cur_blank", None))
                    return 1
                p = wintypes.POINT(*cursor_pos())
                user32.ScreenToClient(hwnd, ctypes.byref(p))
                lpv = (p.x & 0xFFFF) | ((p.y & 0xFFFF) << 16)
                x, y = self.panel_local(lpv)
                w, h = self.springs.get("width", self.panel.w).x, self.springs["height"].x
                inside = 0 <= x < w and 0 <= y < h
                glass_ptr = (inside and self.pointer_style != "system" and not self.bubble_mode
                             and not self.panel.magnifier_active)
                # Direct MiniBlob dragging is intentionally discoverable from
                # the body itself, not advertised with the intrusive four-way
                # move cursor. The palette remains ordinary click-only UI.
                user32.SetCursor(None if glass_ptr else self.cur_arrow)
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
        if getattr(self, "clipboard", None):
            self.clipboard.poll()
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
                    if k.vkCode in (LEFT_CONTROL, RIGHT_CONTROL):
                        states = getattr(self, "dual_control_down", {
                            LEFT_CONTROL: False, RIGHT_CONTROL: False})
                        self.dual_control_down = states
                        states[k.vkCode] = down
                        if down and not self.hotkey_editing and not getattr(self, "dual_control_latched", False) \
                                and states[LEFT_CONTROL] and states[RIGHT_CONTROL]:
                            # Post instead of mutating layered-window styles
                            # inside the hook callback. This works even when a
                            # game owns focus, and the latch prevents repeats.
                            self.dual_control_latched = True
                            user32.PostMessageW(self.hwnd, WM_APP_GAMING_LOCK, 0, 0)
                        elif not down:
                            self.dual_control_latched = False
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
            if getattr(self.panel, "magnifier_active", False):
                self._sync_tool_capture(force=True)
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
    # Keep the familiar script path; v4 has one application host for both views.
    from unified import main
    main()
