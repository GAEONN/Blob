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

    def test_malformed_old_profile_falls_back(self):
        with patch.object(Path, 'read_text', return_value='[]'):
            self.assertEqual(u.initial_config('unused')['app_view'], 'small')
        with patch.object(Path, 'read_text', return_value='{"options": 4}'):
            self.assertFalse(u.initial_config('unused')['options']['soundstart'])

    def test_startup_points_to_one_entry_and_own_registry_value(self):
        with patch.object(u.winreg, 'CreateKey'), patch.object(u.winreg, 'SetValueEx') as write:
            u.set_startup(True)
            self.assertEqual(write.call_args.args[1], 'Blob v4')
        self.assertIn('app.pyw', write.call_args.args[-1])
        self.assertNotIn('dashboard\\app.pyw', write.call_args.args[-1])

    def test_renderer_anchors_follow_mode(self):
        r = u.UnifiedRenderer.__new__(u.UnifiedRenderer)
        r.full = False
        r.W, r.H, r.sp = 680, 796, 28
        self.assertEqual((r.panel_x(248), r.panel_y(216)), (404, 552))
        r.full = True
        self.assertEqual((r.panel_x(92), r.panel_y(480)), (28, 28))

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

    def test_legacy_sources_unchanged_except_entrypoint(self):
        import hashlib
        # Normalized v3 source hashes; works in downloaded archives without Git installed.
        expected = {
            'engine.py': 'c54b82e09a3834f4cc246c54ffc98dc7f0c0573b3fe73c91f52d00ebc4d16327',
            'glass.py': 'dc1e77f8d57cb742afe0e9ab42287ac4db1f0b86e36b0ecfedc77d88d6e6af0f',
            'media.py': '40156698d629532d3edc6fe631e032332c2e0237ebd50fb9af13443f52d96a94',
            'sound.py': '5d2273839d3c0e4bcedbfef77cd0d7113a86f5f497c0413bd867b4b09395c758',
            'applemusic.py': '0f897814de36386b2633e71502f0df43121c059765ad76dd04bcab5bc4e35efe',
            'gaming.py': '57591f2cda910c9dac62306ac18042ae3e140e68e980388ae1bdb011ffb42e1f',
            'reactive.py': 'f7f641cd1a232576b7bd76d59694a4ee018d96d0625e6f476f003373fb7edc9f',
            'blob.pyw': 'd5643ad5b5e3db94630897ba3db167afad7dbd60c00152202c5e0599ccf0e792',
        }
        for name, digest in expected.items():
            source = (u.ROOT/name).read_text(encoding='utf-8').split('if __name__ == "__main__":')[0]
            self.assertEqual(hashlib.sha256(source.encode()).hexdigest(), digest, name)


if __name__ == '__main__':
    unittest.main()
