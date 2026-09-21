"""Blob v4: two presentations, one window and one set of live controllers.

The SmallBlob layout stays stable. This adapter owns mode switching and the v4 profile.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import winreg

from dashboard import dashboard as dash

base, engine, glass, user32 = dash.base, dash.engine, dash.glass, dash.user32
ROOT = Path(__file__).resolve().parent
APP_NAME = 'Blob v4'
MUTEX = 'Local\\BlobUnified-v4'
SMALL, FULL, DOCK, OPEN = 0x8010, 0x8011, 0x8012, 0x8013


def initial_config(local_app_data):
    """Copy preferences, never alter legacy files or auto-enable sound processing."""
    try:
        cfg = json.loads((Path(local_app_data)/'Blob-v3'/'config.json').read_text(encoding='utf-8'))
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    cfg['options'] = dict(cfg['options']) if isinstance(cfg.get('options'), dict) else {}
    cfg['options']['soundstart'] = False
    cfg['app_view'] = 'small'
    cfg.setdefault('overlay_hotkey', {'mods': 3, 'vk': ord('V')})
    return cfg


def set_startup(enable):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base.RUN_KEY) as key:
        if enable:
            pythonw = Path(sys.executable).with_name('pythonw.exe')
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ,
                             f'"{pythonw}" "{ROOT / "app.pyw"}" --startup')
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def configure_runtime():
    engine.DATA_DIR = os.path.join(os.environ['LOCALAPPDATA'], 'Blob-v4')
    engine.CONFIG_PATH = os.path.join(engine.DATA_DIR, 'config.json')
    engine.LOG_PATH = os.path.join(engine.DATA_DIR, 'blob.log')
    os.makedirs(engine.DATA_DIR, exist_ok=True)
    if not Path(engine.CONFIG_PATH).exists():
        engine.save_config(initial_config(os.environ['LOCALAPPDATA']))
    base.APP_NAME = APP_NAME
    base.set_startup = set_startup
    base.Panel = dash.BasePanel
    glass.GlassRenderer = UnifiedRenderer


class UnifiedRenderer(dash.BaseRenderer):
    """Keep the same GL context, changing only buffer size and content anchoring."""
    def __init__(self, *args, **kwargs):
        self.full = False
        original = glass.FRAG
        glass.FRAG = original.replace(
            'uniform float ink_ss;', 'uniform float ink_ss;\nuniform float full_view;')
        glass.FRAG = glass.FRAG.replace(
            'vec2 q = vec2(pp.x, pp.y - (panel_size.y - size.y / ink_ss)) * ink_ss;',
            'vec2 q = vec2(pp.x, pp.y - (1.0-full_view) * (panel_size.y - size.y / ink_ss)) * ink_ss;')
        # Preserve SmallBlob's material, Dashboard's readable binary ink in full view.
        glass.FRAG = glass.FRAG.replace(
            'vec3 ink_col = mix(vec3(1.0), vec3(0.07), ink_dark);',
            'vec3 ic = clamp(col, 0.0, 1.0);\n'
            'vec3 lin = mix(ic/12.92, pow((ic+.055)/1.055, vec3(2.4)), step(vec3(.04045), ic));\n'
            'ink_dark = mix(ink_dark, step(.179, dot(lin, vec3(.2126,.7152,.0722))), full_view);\n'
            'vec3 ink_col = mix(vec3(1.0), vec3(.07*(1.0-full_view)), ink_dark);')
        try:
            super().__init__(*args, **kwargs)
            self.prog['full_view'].value = 0.0
        finally:
            glass.FRAG = original

    def set_view(self, full):
        self.full = full
        self.prog['full_view'].value = float(full)
        self.set_ambient((0, 0, 0, 0))
        self.set_pointer(None, 0, -1, 0)
        self.set_panel_shape(0)
        self.set_bubble_audio(0, 0, 0)
        self.set_viz(None, None, 0)
        self._last_key = None

    def panel_x(self, width):
        return self.sp if self.full else super().panel_x(width)

    def panel_y(self, height):
        return self.sp if self.full else super().panel_y(height)

    resize_surface = dash.DashboardRenderer.resize_surface


def view_method(name):
    """Explicitly route presentation methods without replacing controller state."""
    def call(self, *args, **kwargs):
        owner = dash.DashboardApp if self.full else base.App
        return getattr(owner, name)(self, *args, **kwargs)
    call.__name__ = name
    return call


class UnifiedApp(dash.DashboardApp):
    def __init__(self, requested=None):
        self.full = False
        self.initializing = True
        self.dashboard_pos = None
        self.last_dashboard_text = 0
        self.panels, self.positions = {}, {}
        base.App.__init__(self)  # exactly one Monitor, Sound, NowPlaying, GamingMonitor, AppleMusic
        self.panels['small'] = self.panel
        self.initializing = False
        self.install_menu()
        view = requested or engine.load_config().get('app_view', 'small')
        self.switch_view(view, reveal='--startup' not in sys.argv)

    _panel_y = view_method('_panel_y')
    _schedule = view_method('_schedule')
    _draw_content = view_method('_draw_content')
    _frame = view_method('_frame')
    bubble_drag_key = view_method('bubble_drag_key')
    toggle_overlay_input = view_method('toggle_overlay_input')
    set_music_view = view_method('set_music_view')
    refresh_backdrop = view_method('refresh_backdrop')
    click = view_method('click')

    @property
    def bubble_mode(self):
        owner = dash.DashboardApp if self.full else base.App
        return owner.bubble_mode.fget(self)

    @property
    def bubble_drag_active(self):
        owner = dash.DashboardApp if self.full else base.App
        return owner.bubble_drag_active.fget(self)

    @property
    def overlay_lock_available(self):
        return True

    def apply_gaming_input(self):
        if self.full:
            return dash.DashboardApp.apply_gaming_input(self)
        base.App.apply_gaming_input(self)
        style = user32.GetWindowLongPtrW(self.hwnd, -20)
        user32.SetWindowLongPtrW(self.hwnd, -20, (style | 0x80) & ~0x40000)
        user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x13)

    def show(self):
        if not self.initializing:
            owner = dash.DashboardApp if self.full else base.App
            owner.show(self)

    def install_menu(self):
        post = lambda msg: lambda *args: user32.PostMessageW(self.hwnd, msg, 0, 0)
        item = base.pystray.MenuItem
        self.icon.menu = base.pystray.Menu(
            item('Open', post(base.WM_APP_TOGGLE), default=True, visible=False),
            item('SmallBlob', post(SMALL), checked=lambda _: not self.full, radio=True),
            item('Dashboard', post(FULL), checked=lambda _: self.full, radio=True),
            item('Game dock', post(DOCK)),
            base.pystray.Menu.SEPARATOR,
            item(lambda _: 'Lock overlay' if self.overlay_unlocked else 'Unlock overlay',
                 post(base.WM_APP_GAMING_LOCK), enabled=lambda _: self.visible and self.overlay_lock_available),
            item('Start with Windows', lambda *args: set_startup(not base.startup_enabled()),
                 checked=lambda _: base.startup_enabled()),
            item('Exit Blob', post(base.WM_APP_EXIT)),
        )
        self.icon.update_menu()

    def switch_view(self, view, reveal=True):
        if view not in ('small', 'dashboard'):
            view = 'small'
        current = 'dashboard' if self.full else 'small'
        if current == view:
            if reveal:
                self.show()
            return
        active = self.panel
        lock_state = False if getattr(active, 'dock', False) else getattr(self, '_overlay_unlocked', True)
        self.panels[current] = active
        # A detached dock must not replace the overview's saved position.
        self.positions[current] = list(self.dashboard_pos if self.full and active.dock
                                       and self.dashboard_pos else self.pos) if self.pos else None
        self.hide()
        if user32.GetCapture() == self.hwnd:
            user32.ReleaseCapture()
        if view not in self.panels:
            self.panels[view] = dash.DashboardPanel(self.S)
            self.panels[view].settings_note = 'Shared with SmallBlob.'
        self.panel = self.panels[view]
        self.panel.am = self.am
        self.panel.options = dict(active.options)
        self.panel.backdrop = active.backdrop
        self.panel.query = active.query
        self.panel.hotkey_label = base.DUAL_CONTROL_LABEL
        self.full = view == 'dashboard'
        self.glass.set_view(self.full)
        self.pos = self.positions.get(view)
        self._overlay_unlocked = lock_state
        self.gaming_modifier_drag = False
        if self.full:
            self.panel.dock = self.panel.dock_open = False
            self.panel.page = 'music'
            if self.pos is None:
                self.pos = list(self.positions.get(current) or base.cursor_pos())
            self.fit_dashboard_to_monitor(self.panel.fullscreen)
            width, height = round(self.panel.window_w*self.S), round(self.panel.window_h*self.S)
        else:
            width, height = round(dash.BasePanel.GAMING_WIDE*self.S), round(self.panel.max_height()/self.ss)
        self.panel.update_width()
        self.glass.resize_surface(width, height)
        self.hardware_top = self.glass.panel_y(round((210+dash.BasePanel.TOP)*self.S))
        self.music_top = self.glass.panel_y(round((dash.BasePanel.TOP+118)*self.S))
        self.controls, self.old_controls = [], []
        self.springs = base.Springs()
        self.drag = self.drag_click = self.drag_origin = self.slider_drag = self.pressed = None
        self.hover = self.mouse_xy = self.attached = self.detaching = None
        self.drag_moved = self.mouse_in = False
        self.seek_value = None
        self.hover_since = (None, 0.0)
        self.media_seen = None
        self.last_frame = self.last_text = self.last_dashboard_text = 0
        self.glass.source.frozen = self.captureable
        self.draw_content()
        for key in ('height', 'width'):
            spring = self.springs[key]
            spring.x, spring.v = spring.target, 0
        self.apply_gaming_input()
        cfg = engine.load_config()
        cfg['app_view'] = view
        engine.save_config(cfg)
        self.icon.update_menu()
        if reveal:
            self.show()

    def wndproc(self, hwnd, msg, wp, lp):
        if msg == OPEN:
            self.show()
            return 0
        if msg in (SMALL, FULL, DOCK):
            self.switch_view('small' if msg == SMALL else 'dashboard')
            if msg == DOCK and not self.panel.dock:
                self.enter_dock()
            return 0
        if msg == 0x10:
            self.quit()
            return 0
        # F10 is local to Blob, never a global game key. Ignore key autorepeat.
        if msg in (base.WM_KEYDOWN, 0x104) and wp == 0x79 and not self.hotkey_editing:
            if not lp & (1 << 30) and user32.GetForegroundWindow() == hwnd:
                self.switch_view('small' if self.full else 'dashboard')
            return 0
        owner = dash.DashboardApp if self.full else base.App
        return owner.wndproc(self, hwnd, msg, wp, lp)


def main():
    parser = argparse.ArgumentParser(description='Blob v4: SmallBlob and Dashboard')
    parser.add_argument('--view', choices=('small', 'dashboard'))
    parser.add_argument('--startup', action='store_true')
    args = parser.parse_args()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass
    base.k32.CreateMutexW.restype = base.wintypes.HANDLE
    base.k32.CloseHandle.argtypes = [base.wintypes.HANDLE]
    mutex = base.k32.CreateMutexW(None, False, MUTEX)
    if not mutex:
        raise ctypes.WinError()
    if base.k32.GetLastError() == 183:
        user32.FindWindowW.restype = base.wintypes.HWND
        user32.FindWindowW.argtypes = [base.wintypes.LPCWSTR, base.wintypes.LPCWSTR]
        hwnd = user32.FindWindowW('BlobGlass', APP_NAME)
        if hwnd and not args.startup:
            # A second launcher requests the existing window, never another audio engine.
            message = FULL if args.view == 'dashboard' else SMALL if args.view == 'small' else OPEN
            user32.PostMessageW(hwnd, message, 0, 0)
        base.k32.CloseHandle(mutex)
        return
    configure_runtime()
    try:
        UnifiedApp(args.view).run()
    except Exception:
        import traceback
        engine.log('fatal: '+traceback.format_exc())
        raise
    finally:
        base.k32.CloseHandle(mutex)


if __name__ == '__main__':
    main()
