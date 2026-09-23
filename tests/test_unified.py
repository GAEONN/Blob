"""Offline host transitions: native calls mocked, no app or audio controllers started."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import test_regressions as reg
import unified as u


class UnifiedTests(unittest.TestCase):
    def app(self):
        a = u.UnifiedApp.__new__(u.UnifiedApp)
        a.full = a.initializing = False
        a.S = 1
        a.ss = 2
        a.panel = u.dash.BasePanel(1)
        a.panel.page = 'music'
        a.panel.set_compact(True)
        a.snap, a.sound, a.media, a.am = reg.fixtures()
        a.mon = Mock()
        a.gaming = Mock(snapshot=Mock(return_value=dict(fps=144, frame_ms=6.94)))
        a.panels = {'small': a.panel}
        a.positions = {}
        a.pos = [400, 300]
        a.dashboard_pos = None
        a.glass = Mock(sp=28, source=NS(frozen=False))
        a.glass.panel_y.side_effect = lambda h: 768-h
        a.hwnd = 123
        a.cur_arrow = 4
        a.visible = a._overlay_unlocked = True
        a.captureable = a.startup = False
        a.glassiness = .35
        a.pointer_style = 'system'
        a.hotkey_mods, a.hotkey_vk = 3, ord('V')
        a.hotkey_editing = False
        a.icon = Mock()
        a.hide = Mock(side_effect=lambda: setattr(a, 'visible', False))
        a.show = Mock()
        return a

    def setUp(self):
        self.win = patch.object(u, 'user32').start()
        self.addCleanup(patch.stopall)
        # dash methods access the same native binding, but replace its module reference too.
        patch.object(u.dash, 'user32', self.win).start()
        patch.object(u.base, 'user32', self.win).start()
        self.win.GetWindowLongPtrW.return_value = 0x80088
        self.win.GetCapture.return_value = 0
        patch.object(u.base, 'work_area_at', return_value=(NS(left=0, top=0, right=1920, bottom=1040), None)).start()
        patch.object(u.engine, 'load_config', return_value={}).start()
        self.save = patch.object(u.engine, 'save_config').start()

    def test_repeated_switches_reuse_services_and_small_panel(self):
        a = self.app()
        original = a.panel
        services = [a.mon, a.sound, a.media, a.am, a.gaming, a.glass, a.icon]
        for _ in range(4):
            a.switch_view('dashboard')
            self.assertTrue(a.full)
            self.assertEqual(a.panel.page, 'music')
            self.assertEqual(a.panel.window_w, 1896)
            self.assertTrue({'music', 'system', 'sound', 'gaming'} <= a.panel.regions.keys())
            a.panel.options['musicreactive'] = False
            a.switch_view('small')
            self.assertFalse(a.full)
            self.assertIs(a.panel, original)
            self.assertTrue(a.panel.compact)
            self.assertEqual(a.panel.w, 248*2)
            self.assertFalse(a.panel.options['musicreactive'])
            self.assertEqual(services, [a.mon, a.sound, a.media, a.am, a.gaming, a.glass, a.icon])
        self.assertEqual(a.positions['small'], [400, 300])

    def test_return_from_dock_restores_overview_and_locks_small_gaming(self):
        a = self.app()
        a.panel.page = 'gaming'
        a.switch_view('dashboard')
        a.dashboard_pos = list(a.pos)
        a.panel.dock = True
        a.pos = [10, 50]
        a.switch_view('small')
        self.assertFalse(a.overlay_unlocked)
        a.switch_view('dashboard')
        self.assertFalse(a.panel.dock)
        self.assertFalse(a.overlay_unlocked)
        self.assertEqual(a.panel.page, 'music')

    def test_switch_hidden_startup_never_shows_window(self):
        a = self.app()
        a.switch_view('dashboard', reveal=False)
        a.show.assert_not_called()

    def test_f10_local_only_and_no_repeat(self):
        a = self.app()
        a.switch_view = Mock()
        self.win.GetForegroundWindow.return_value = 999
        a.wndproc(123, 0x104, 0x79, 0)
        a.switch_view.assert_not_called()
        self.win.GetForegroundWindow.return_value = 123
        a.wndproc(123, 0x104, 0x79, 1 << 30)
        a.switch_view.assert_not_called()
        a.wndproc(123, 0x104, 0x79, 0)
        a.switch_view.assert_called_once_with('dashboard')

    def test_mode_messages_and_shared_tray(self):
        a = self.app()
        a.install_menu()
        items = list(a.icon.menu.items)
        self.assertIn('SmallBlob', [i.text for i in items])
        self.assertIn('Dashboard', [i.text for i in items])
        a.switch_view = Mock()
        a.wndproc(123, u.FULL, 0, 0)
        a.switch_view.assert_called_once_with('dashboard')

    def test_first_run_migration_does_not_change_v3(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp)/'Blob-v3'/'config.json'
            old.parent.mkdir()
            # Test-only fixture, not the real user profile.
            old.write_text(json.dumps({'glass': .31, 'compact': True, 'options': {'soundstart': True}}))
            before = old.read_bytes()
            result = u.initial_config(tmp)
            self.assertEqual(result['glass'], .31)
            self.assertTrue(result['compact'])
            self.assertFalse(result['options']['soundstart'])
            self.assertEqual(old.read_bytes(), before)

    def test_first_run_migration_prefers_v4_without_mutating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            v4 = Path(tmp)/'Blob-v4'/'config.json'
            v3 = Path(tmp)/'Blob-v3'/'config.json'
            v4.parent.mkdir()
            v3.parent.mkdir()
            v4.write_text(json.dumps({'glass': .47, 'compact': False, 'options': {'soundstart': True}}))
            v3.write_text(json.dumps({'glass': .21, 'compact': True}))
            before = v4.read_bytes()
            result = u.initial_config(tmp)
            self.assertEqual(result['glass'], .47)
            self.assertFalse(result['compact'])
            self.assertFalse(result['options']['soundstart'])
            self.assertEqual(v4.read_bytes(), before)

    def test_malformed_old_profile_falls_back(self):
        with patch.object(Path, 'read_text', return_value='[]'):
            self.assertEqual(u.initial_config('unused')['app_view'], 'small')
        with patch.object(Path, 'read_text', return_value='{"options": 4}'):
            self.assertFalse(u.initial_config('unused')['options']['soundstart'])

    def test_startup_points_to_one_entry_and_own_registry_value(self):
        with patch.object(u.winreg, 'CreateKey'), patch.object(u.winreg, 'SetValueEx') as write:
            u.set_startup(True)
            self.assertEqual(write.call_args.args[1], 'Blob v5')
        self.assertIn('app.pyw', write.call_args.args[-1])
        self.assertNotIn('dashboard\\app.pyw', write.call_args.args[-1])

    def test_renderer_anchors_follow_mode(self):
        r = u.UnifiedRenderer.__new__(u.UnifiedRenderer)
        r.full = False
        r.W, r.H, r.sp = 680, 796, 28
        self.assertEqual((r.panel_x(248), r.panel_y(216)), (404, 552))
        r.full = True
        self.assertEqual((r.panel_x(92), r.panel_y(480)), (28, 28))

    def test_screen_capture_rounds_fractional_window_coordinates(self):
        import numpy as np
        source = u.glass.ScreenSource.__new__(u.glass.ScreenSource)
        source.frozen = False
        source.outputs = []
        source.screen_dc = 123
        pixels = np.zeros((8, 10, 4), dtype=np.uint8)
        with patch.object(u.glass, 'gdi_grab') as grab:
            u.glass.ScreenSource.grab(source, 12.4, 33.6, 10.0, 8.0, pixels, True)
        self.assertEqual(grab.call_args.args[1:5], (12, 34, 10, 8))

    def test_dashboard_exposes_smallblob_and_minimize_controls(self):
        p = u.dash.DashboardPanel(1)
        snap, sound, media, am = reg.fixtures()
        p.am = am
        p.game = {'fps': 144, 'frame_ms': 6.94, 'ram_percent': 62, 'ram_gb': 19.8}
        p.draw(snap, sound, .35, False, media=media)
        self.assertIn('dash:small', p.rects)
        self.assertIn('dash:minimize', p.rects)
        self.assertEqual(p.hit(*((p.rects['dash:small'][0] + p.rects['dash:small'][2]) / 2,
                                 (p.rects['dash:small'][1] + p.rects['dash:small'][3]) / 2)),
                         'dash:small')

    def test_real_gpu_compiles_and_resizes_without_window_or_capture(self):
        import numpy as np
        with patch.object(u.glass, 'ScreenSource', return_value=NS(frozen=False)):
            r = u.UnifiedRenderer(1, 624, 740)
        try:
            r.set_supersample(2)
            for full, width, height in ((False, 624, 740), (True, 1120, 740),
                                        (True, 92, 480), (False, 624, 740)):
                r.set_view(full)
                r.resize_surface(width, height)
                self.assertEqual(r.prog['full_view'].value, float(full))
                r.cap[:] = [120, 140, 160, 255]
                p = u.dash.DashboardPanel(1) if full else u.dash.BasePanel(1)
                p.am = reg.fixtures()[3]
                if full:
                    p.dock, p.dock_open = width == 92, width == 92
                    p.window_w, p.window_h = width, height
                p.draw(*reg.fixtures()[:2], .35, False, media=reg.fixtures()[2])
                r.set_content(p.ink, p.accent, p.pic, instant=True)
                with patch.object(u.glass.user32, 'UpdateLayeredWindow', return_value=True):
                    r.render(None, 0, 0, p.w/2, p.height(reg.fixtures()[0])/2, 1, (-.55,-.83), .35)
                self.assertGreater(np.asarray(r.dib.arr)[...,3].sum(), 0)
        finally:
            r.dib.free()
            r.ctx.release()

    def test_initialization_constructs_each_controller_once(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            controllers = [stack.enter_context(patch.object(owner, name)) for owner, name in
                           ((u.engine,'Monitor'),(u.base,'Sound'),(u.base,'NowPlaying'),
                            (u.base,'GamingMonitor'),(u.base,'AppleMusic'))]
            stack.enter_context(patch.object(u.base.threading, 'Thread'))
            stack.enter_context(patch.object(u.base, 'startup_enabled', return_value=False))
            stack.enter_context(patch.object(u.base.ctypes.windll.winmm, 'timeBeginPeriod'))
            stack.enter_context(patch.object(u.base.k32, 'GetModuleHandleW', return_value=1))
            stack.enter_context(patch.object(u.base.pystray, 'Icon'))
            stack.enter_context(patch.object(u.glass, 'GlassRenderer', return_value=Mock(sp=28)))
            stack.enter_context(patch.object(u.UnifiedApp, 'switch_view'))
            stack.enter_context(patch.object(u.sys, 'argv', ['app.pyw', '--startup']))
            self.win.GetDpiForSystem.return_value = 96
            self.win.LoadCursorW.return_value = 1
            u.UnifiedApp()
        for controller in controllers:
            controller.assert_called_once()

    def test_shared_sources_match_the_current_unified_profile(self):
        import hashlib
        # Normalized shared-source hashes; works in downloaded archives without Git installed.
        expected = {
            'engine.py': 'fddf6cd47f896967851540034a6f787732f832a3ba962d50e1865d45ad7f37c3',
            'glass.py': '8380a894dfab1a75dc45131f6b5f122783c50f65d6342c5add63528fada7f8f2',
                           'media.py': '746b402550e783cd195076795f5f9aacaa7bf6c7a30155c2f6153f144fe92807',
            'sound.py': 'cdb5fc4bd15a60f2509415de68fd6cea61e219ca8888ec078c5b92e909f874b1',
            'applemusic.py': '0f897814de36386b2633e71502f0df43121c059765ad76dd04bcab5bc4e35efe',
            'gaming.py': '57591f2cda910c9dac62306ac18042ae3e140e68e980388ae1bdb011ffb42e1f',
            'reactive.py': 'f7f641cd1a232576b7bd76d59694a4ee018d96d0625e6f476f003373fb7edc9f',
                           'blob.pyw': 'c24984c34cac6910fefe150237596ffe9dc4923676420a456662970d696ae473',
        }
        for name, digest in expected.items():
            source = (u.ROOT/name).read_text(encoding='utf-8').split('if __name__ == "__main__":')[0]
            self.assertEqual(hashlib.sha256(source.encode()).hexdigest(), digest, name)


if __name__ == '__main__':
    unittest.main()
