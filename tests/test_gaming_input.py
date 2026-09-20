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
        a.visible, a.gaming_unlocked = True, False
        a.drag, a.slider_drag, a.pressed = (10, 10), None, "tabs"
        a.hover, a.mouse_xy, a.mouse_in = "tabs", (12, 12), True
        a.attached, a.detaching = "tabs", None
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

    def test_other_views_regain_normal_input(self):
        self.u.GetWindowLongPtrW.return_value = self.base | blob.WS_EX_TRANSPARENT | blob.WS_EX_NOACTIVATE
        self.app.panel.page = "music"
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

    def test_hotkey_and_tray_use_same_toggle_and_ignore_other_views(self):
        self.app.wndproc(101, blob.WM_HOTKEY, blob.GAMING_HOTKEY, 0)
        self.assertTrue(self.app.gaming_unlocked)
        self.app.wndproc(101, blob.WM_APP_GAMING_LOCK, 0, 0)
        self.assertFalse(self.app.gaming_unlocked)
        self.app.panel.page = "sound"
        self.app.wndproc(101, blob.WM_APP_GAMING_LOCK, 0, 0)
        self.assertFalse(self.app.gaming_unlocked)
        self.app.panel.page, self.app.visible = "gaming", False
        self.app.wndproc(101, blob.WM_APP_GAMING_LOCK, 0, 0)
        self.assertFalse(self.app.gaming_unlocked)

    def test_music_callback_never_focuses_gaming_even_unlocked(self):
        for unlocked in (False, True):
            self.app.gaming_unlocked = unlocked
            self.app.wndproc(101, blob.WM_APP_REFOCUS, 0, 0)
        self.u.SetForegroundWindow.assert_not_called()
        self.app.panel.page = "music"
        self.app.wndproc(101, blob.WM_APP_REFOCUS, 0, 0)
        self.u.SetForegroundWindow.assert_called_once_with(101)

    def test_show_gaming_relocks_without_requesting_focus(self):
        a = self.app
        a.gaming_unlocked, a.pinned, a.captureable = True, True, False
        a.mon, a.draw_content, a.frame, a._schedule = Mock(), Mock(), Mock(), Mock()
        a.snap, a.pos = {}, [10, 10]
        a.springs = {"height": NS(x=0, target=86)}
        a.show()
        self.assertTrue(a.gaming_locked)
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
