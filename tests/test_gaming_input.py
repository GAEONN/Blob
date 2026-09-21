"""Gaming must not intercept game input; editing is an explicit, reversible mode."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import test_regressions as reg

blob = reg.blob


class GamingInputTests(unittest.TestCase):
    def setUp(self):
        self.app = blob.App.__new__(blob.App)
        a = self.app
        a.hwnd, a.cur_arrow = 101, 202
        a.panel = NS(page="gaming", tabs_open=True)
        a.visible, a.gaming_unlocked, a.gaming_modifier_drag = True, False, False
        a.drag, a.slider_drag, a.pressed = (10, 10), None, "tabs"
        a.drag_click, a.drag_origin, a.drag_moved = None, None, False
        a.music_press_active = a.music_hold_fired = a.music_click_pending = False
        a.hover, a.mouse_xy, a.mouse_in = "tabs", (12, 12), True
        a.attached, a.detaching = "tabs", None
        a.cur_move = 303
        a.icon = Mock()
        self.patcher = patch.object(blob, "user32")
        self.u = self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.base = 0x80000 | 0x80 | 0x8
        self.u.GetWindowLongPtrW.return_value = self.base
        self.u.GetCapture.return_value = a.hwnd
        self.u.GetForegroundWindow.return_value = a.hwnd

    def test_lock_preserves_layered_topmost_and_passes_input(self):
        self.app.apply_gaming_input()
        self.u.SetWindowLongPtrW.assert_called_once_with(
            101, -20, self.base | blob.WS_EX_TRANSPARENT | blob.WS_EX_NOACTIVATE)
        self.u.ReleaseCapture.assert_called_once()
        self.assertIsNone(self.app.drag)
        self.assertIsNone(self.app.pressed)
        self.assertIsNone(self.app.attached)
        self.assertFalse(self.app.mouse_in)
        self.assertFalse(self.app.panel.tabs_open)

    def test_does_not_release_other_window_capture(self):
        self.u.GetCapture.return_value = 999
        self.app.apply_gaming_input()
        self.u.ReleaseCapture.assert_not_called()

    def test_unlock_keeps_noactivate_and_exposes_navigation(self):
        self.app.toggle_gaming_input()
        self.assertTrue(self.app.gaming_unlocked)
        self.assertTrue(self.app.panel.tabs_open)
        self.u.SetWindowLongPtrW.assert_called_once_with(101, -20, self.base | blob.WS_EX_NOACTIVATE)
        self.u.SetForegroundWindow.assert_not_called()
        self.app.toggle_gaming_input()
        self.assertTrue(self.app.gaming_locked)
        self.assertFalse(self.app.panel.tabs_open)

    def test_ctrl_alt_temporarily_enables_drag_without_unlocking(self):
        a = self.app
        a.drag, a.pos, a.captureable = None, [100, 80], False
        a.pinned, a.vel, a.last_frame = False, blob.np.zeros(2), 0
        a.frame = Mock()
        self.u.GetAsyncKeyState.side_effect = lambda vk: 0x8000 if vk in (0x11, 0x12) else 0

        a.update_gaming_modifier_drag()

        self.assertTrue(a.gaming_drag_active)
        self.assertFalse(a.gaming_unlocked)
        self.u.SetWindowLongPtrW.assert_called_once_with(101, -20, self.base | blob.WS_EX_NOACTIVATE)
        with patch.object(blob, "cursor_pos", side_effect=[(130, 100), (170, 130)]):
            self.assertEqual(a.wndproc(101, blob.WM_LBUTTONDOWN, 0, 0), 0)
            self.assertEqual(a.wndproc(101, blob.WM_MOUSEMOVE, 0, 0), 0)
        self.assertEqual(a.pos, [140, 110])
        self.assertTrue(a.pinned)
        self.u.SetCapture.assert_called_once_with(101)

        self.assertEqual(a.wndproc(101, blob.WM_LBUTTONUP, 0, 0), 0)
        self.assertIsNone(a.drag)
        self.u.ReleaseCapture.assert_called_once()

    def test_releasing_ctrl_alt_restores_click_through(self):
        self.app.gaming_modifier_drag = True
        self.app.drag = (10, 10)
        self.u.GetAsyncKeyState.return_value = 0

        self.app.update_gaming_modifier_drag()

        self.assertFalse(self.app.gaming_modifier_drag)
        self.assertIsNone(self.app.drag)
        self.u.SetWindowLongPtrW.assert_called_once_with(
            101, -20, self.base | blob.WS_EX_TRANSPARENT | blob.WS_EX_NOACTIVATE)

    def test_lock_state_applies_to_non_gaming_views(self):
        self.u.GetWindowLongPtrW.return_value = self.base | blob.WS_EX_TRANSPARENT | blob.WS_EX_NOACTIVATE
        self.app.panel.page = "music"
        self.app.apply_gaming_input()
        self.u.SetWindowLongPtrW.assert_called_once_with(
            101, -20, self.base | blob.WS_EX_TRANSPARENT)
        self.u.SetWindowLongPtrW.reset_mock()
        self.u.GetWindowLongPtrW.return_value = self.base | blob.WS_EX_TRANSPARENT
        self.app.gaming_unlocked = True
        self.app.apply_gaming_input()
        self.u.SetWindowLongPtrW.assert_called_once_with(101, -20, self.base)

    def test_stale_mouse_events_cannot_click_drag_or_hide_cursor(self):
        self.app.click = Mock()
        self.app.panel_local = Mock()
        for msg in (blob.WM_LBUTTONDOWN, blob.WM_LBUTTONUP, blob.WM_MOUSEMOVE, 0x20A, blob.WM_SETCURSOR):
            self.assertEqual(self.app.wndproc(101, msg, 0, 0), 0)
        self.app.click.assert_not_called()
        self.app.panel_local.assert_not_called()
        self.u.SetCapture.assert_not_called()
        self.u.SetCursor.assert_not_called()
        self.assertEqual(self.app.wndproc(101, 0x84, 0, 0), -1)

    def test_hotkey_and_tray_toggle_one_state_on_every_view(self):
        self.app.wndproc(101, blob.WM_HOTKEY, blob.GAMING_HOTKEY, 0)
        self.assertTrue(self.app.gaming_unlocked)
        self.app.wndproc(101, blob.WM_APP_GAMING_LOCK, 0, 0)
        self.assertFalse(self.app.gaming_unlocked)
        self.app.panel.page = "sound"
        self.app.wndproc(101, blob.WM_APP_GAMING_LOCK, 0, 0)
        self.assertTrue(self.app.gaming_unlocked)
        self.app.panel.page, self.app.visible = "gaming", False
        self.app.wndproc(101, blob.WM_APP_GAMING_LOCK, 0, 0)
        self.assertTrue(self.app.gaming_unlocked)

    def test_tab_changes_preserve_global_lock_state(self):
        a = self.app
        a.panel = NS(page="blob", music_menu=None, tabs_open=False, tabs_t=0,
                     update_width=Mock())
        a.gaming_unlocked = True
        a.glass = NS(set_ambient=Mock())
        a.media = NS(art=None)
        a.media_seen = None
        a.old_page_springs = a.apply_gaming_input = Mock()
        a.springs = {}
        a.sound = NS(refresh_devices=Mock())
        a.draw_content = a.frame = a._schedule = Mock()
        with patch.object(blob, "startup_enabled", return_value=False):
            a.click("page:gaming", 0)
            self.assertTrue(a.overlay_unlocked)
            a.toggle_overlay_input()
            self.assertTrue(a.overlay_locked)
            a.click("page:sound", 0)
            self.assertTrue(a.overlay_locked)

    def test_same_hotkey_unlocks_hardware_bubble_for_dragging(self):
        a = self.app
        a.panel = NS(page="blob", hardware_view="bubble", tabs_open=False)
        a.bubble_unlocked = False
        a.frame_dirty = False
        a.wndproc(101, blob.WM_HOTKEY, blob.GAMING_HOTKEY, 0)
        self.assertTrue(a.bubble_unlocked)
        self.assertTrue(a.bubble_drag_active)
        self.assertTrue(a.frame_dirty)
        a.wndproc(101, blob.WM_APP_GAMING_LOCK, 0, 0)
        self.assertFalse(a.bubble_unlocked)

    def test_same_hotkey_unlocks_music_bubble_for_dragging(self):
        a = self.app
        a.panel = NS(page="music", music_view="bubble", tabs_open=False)
        a.hover = "mview:now"
        a.bubble_unlocked = False
        a.frame_dirty = False
        a.wndproc(101, blob.WM_HOTKEY, blob.GAMING_HOTKEY, 0)
        self.assertTrue(a.bubble_unlocked)
        self.assertTrue(a.bubble_drag_active)
        self.assertTrue(a.frame_dirty)
        a.wndproc(101, blob.WM_APP_GAMING_LOCK, 0, 0)
        self.assertFalse(a.bubble_unlocked)

    def test_unlocked_bubble_distinguishes_click_from_drag(self):
        a = self.app
        a.panel = NS(page="blob", hardware_view="bubble", tabs_open=False,
                     hit=Mock(return_value="hcycle"))
        a.bubble_unlocked, a.drag = True, None
        a.panel_local = Mock(return_value=(20, 20))
        a.click, a._schedule, a.frame = Mock(), Mock(), Mock()
        a.pos, a.pinned, a.captureable = [100, 80], False, False
        a.vel, a.last_frame = blob.np.zeros(2), 0

        with patch.object(blob, "cursor_pos", side_effect=[(130, 100), (132, 101)]):
            a.wndproc(101, blob.WM_LBUTTONDOWN, 0, 0)
            a.wndproc(101, blob.WM_MOUSEMOVE, 0, 0)
            a.wndproc(101, blob.WM_LBUTTONUP, 0, 0)
        a.click.assert_called_once_with("hcycle", 0)
        self.assertEqual(a.pos, [100, 80])

        a.click.reset_mock()
        with patch.object(blob, "cursor_pos", side_effect=[(130, 100), (170, 130)]):
            a.wndproc(101, blob.WM_LBUTTONDOWN, 0, 0)
            a.wndproc(101, blob.WM_MOUSEMOVE, 0, 0)
            a.wndproc(101, blob.WM_LBUTTONUP, 0, 0)
        a.click.assert_not_called()
        self.assertEqual(a.pos, [140, 110])
        self.assertTrue(a.pinned)

    def test_unlocked_music_bubble_main_keeps_playback_gestures(self):
        a = self.app
        a.panel = NS(page="music", music_view="bubble", tabs_open=False,
                     hit=Mock(return_value="mbubble:gesture"))
        a.bubble_unlocked, a.drag = True, None
        a.panel_local = Mock(return_value=(43, 55))
        a.start_music_bubble_press = Mock()
        a.pos = [100, 80]
        with patch.object(blob, "cursor_pos", return_value=(130, 100)):
            a.wndproc(101, blob.WM_LBUTTONDOWN, 0, 0)
        self.assertIsNone(a.drag)
        a.start_music_bubble_press.assert_called_once_with()

    def test_unlocked_music_satellite_clicks_or_drags(self):
        a = self.app
        a.panel = NS(page="music", music_view="bubble", tabs_open=False,
                     hit=Mock(return_value="mview:now"))
        a.bubble_unlocked, a.drag = True, None
        a.panel_local = Mock(return_value=(79, 22))
        a.click, a._schedule, a.frame = Mock(), Mock(), Mock()
        a.pos, a.pinned, a.captureable = [100, 80], False, False
        a.vel, a.last_frame = blob.np.zeros(2), 0

        with patch.object(blob, "cursor_pos", side_effect=[(130, 100), (132, 101)]):
            a.wndproc(101, blob.WM_LBUTTONDOWN, 0, 0)
            a.wndproc(101, blob.WM_MOUSEMOVE, 0, 0)
            a.wndproc(101, blob.WM_LBUTTONUP, 0, 0)
        a.click.assert_called_once_with("mview:now", 0)

        a.click.reset_mock()
        with patch.object(blob, "cursor_pos", side_effect=[(130, 100), (170, 130)]):
            a.wndproc(101, blob.WM_LBUTTONDOWN, 0, 0)
            a.wndproc(101, blob.WM_MOUSEMOVE, 0, 0)
            a.wndproc(101, blob.WM_LBUTTONUP, 0, 0)
        a.click.assert_not_called()
        self.assertEqual(a.pos, [140, 110])

    def test_hotkey_editor_re_registers_and_persists_binding(self):
        a = self.app
        a.hotkey_mods, a.hotkey_vk = blob.DEFAULT_HOTKEY
        a.gaming_hotkey_registered = True
        a.panel.hotkey_label, a.panel.hotkey_error = "Ctrl + Alt + G", ""
        self.u.RegisterHotKey.return_value = True
        config = {}
        with patch.object(blob.engine, "load_config", return_value=config), \
             patch.object(blob.engine, "save_config") as save:
            self.assertTrue(a.set_overlay_hotkey(blob.MOD_CONTROL | blob.MOD_SHIFT, ord("K")))
        self.u.UnregisterHotKey.assert_called_once_with(101, blob.GAMING_HOTKEY)
        self.u.RegisterHotKey.assert_called_once_with(
            101, blob.GAMING_HOTKEY, blob.MOD_CONTROL | blob.MOD_SHIFT | blob.MOD_NOREPEAT, ord("K"))
        self.assertEqual(a.panel.hotkey_label, "Ctrl + Shift + K")
        self.assertEqual(config["overlay_hotkey"], {"mods": 6, "vk": ord("K")})
        save.assert_called_once_with(config)

    def test_music_callback_never_focuses_gaming_even_unlocked(self):
        for unlocked in (False, True):
            self.app.gaming_unlocked = unlocked
            self.app.wndproc(101, blob.WM_APP_REFOCUS, 0, 0)
        self.u.SetForegroundWindow.assert_not_called()
        self.app.panel.page = "music"
        self.app.wndproc(101, blob.WM_APP_REFOCUS, 0, 0)
        self.u.SetForegroundWindow.assert_called_once_with(101)

    def test_show_gaming_preserves_unlock_without_requesting_focus(self):
        a = self.app
        a.gaming_unlocked, a.pinned, a.captureable = True, True, False
        a.mon, a.draw_content, a.frame, a._schedule = Mock(), Mock(), Mock(), Mock()
        a.snap, a.pos = {}, [10, 10]
        a.springs = {"height": NS(x=0, target=86)}
        a.show()
        self.assertFalse(a.gaming_locked)
        self.assertTrue(a.overlay_unlocked)
        self.u.ShowWindow.assert_called_once_with(101, 4)
        self.u.SetForegroundWindow.assert_not_called()

    def test_topmost_game_above_strip_is_reordered_without_activation(self):
        self.u.GetForegroundWindow.return_value = 500
        self.u.GetWindow.side_effect = [400, 500]
        self.app.maintain_gaming_topmost()
        self.u.SetWindowPos.assert_called_once_with(101, -1, 0, 0, 0, 0, 0x213)
        self.u.SetForegroundWindow.assert_not_called()
        self.u.SetWindowLongPtrW.assert_not_called()

    def test_does_not_reorder_when_already_above_game(self):
        self.u.GetForegroundWindow.return_value = 500
        self.u.GetWindow.return_value = 0
        self.app.maintain_gaming_topmost()
        self.u.SetWindowPos.assert_not_called()

    def test_regular_foreground_window_does_not_trigger_reorder(self):
        self.u.GetForegroundWindow.return_value = 500
        self.u.GetWindowLongPtrW.side_effect = [self.base, 0]
        self.app.maintain_gaming_topmost()
        self.u.GetWindow.assert_not_called()
        self.u.SetWindowPos.assert_not_called()

    def test_hidden_and_other_views_never_force_topmost(self):
        self.u.GetForegroundWindow.return_value = 500
        self.app.visible = False
        self.app.maintain_gaming_topmost()
        self.app.visible, self.app.panel.page = True, "music"
        self.app.maintain_gaming_topmost()
        self.u.SetWindowPos.assert_not_called()
        self.u.GetForegroundWindow.assert_not_called()

    def test_lost_topmost_style_is_restored(self):
        self.u.GetForegroundWindow.return_value = 500
        self.u.GetWindowLongPtrW.return_value = self.base & ~0x8
        self.app.maintain_gaming_topmost()
        self.u.SetWindowPos.assert_called_once_with(101, -1, 0, 0, 0, 0, 0x213)


if __name__ == "__main__":
    unittest.main()
