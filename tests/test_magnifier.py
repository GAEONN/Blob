"""Magnifier behaviour and real GPU sampling (no live screen or mouse access)."""
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import unittest
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from test_regressions import blob, glass, RecordingPanel, fixtures


def render_demo(output=None):
    snap, sound, media, _ = fixtures()
    panel = RecordingPanel(1)
    panel.tools_open, panel.tool_view = True, "magnifier"
    panel.draw(snap, sound, .4, False, media=media)
    with patch.object(glass, "ScreenSource", return_value=NS(frozen=False)):
        renderer = glass.GlassRenderer(1, 324, 324)
    renderer.set_supersample(2)
    hh, ww = renderer.cap.shape[:2]
    desktop = Image.new("RGB", (ww, hh), (246, 246, 244))
    draw = ImageDraw.Draw(desktop)
    font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 11)
    for y in range(12, hh, 20):
        draw.text((12, y), "Small text is easier to read with the glass magnifier. 0123456789", font=font, fill=(28, 28, 28))
    renderer.cap[..., :3] = np.asarray(desktop)[..., ::-1]
    renderer.cap[..., 3] = 255
    renderer.set_content(panel.ink, panel.accent, panel.pic, instant=True)
    app = blob.App.__new__(blob.App)
    app.panel, app.springs, app.S = panel, blob.Springs(), 1
    app.attached = app.detaching = app.hover = app.pressed = app.slider_drag = None
    app.pointer_style = "system"
    lenses, _ = app.resolve(panel.controls, 1, "")
    for lens in lenses:
        lens["rect"] = tuple(v / 2 for v in lens["rect"])
        for key in ("r", "bevel", "strength"):
            if key in lens:
                lens[key] /= 2
    renderer.set_lenses(lenses)
    frames = []
    for zoom in (1.0, 2.0, 4.0):
        panel.magnifier_zoom = zoom
        panel.draw(snap, sound, .4, False, media=media)
        renderer.set_content(panel.ink, panel.accent, panel.pic, instant=True)
        renderer.set_magnifier(tuple(v / 2 for v in panel.magnifier_aperture()), zoom)
        with patch.object(glass.user32, "UpdateLayeredWindow", return_value=True):
            renderer.render(None, 0, 0, 324, 324, 1, (-.55, -.83), .6, panel_y=renderer.sp)
        frames.append(renderer.dib.arr.copy())
    if output:
        sheet = Image.new("RGB", (renderer.W * 3, renderer.H + 28), (32, 32, 32))
        d = ImageDraw.Draw(sheet)
        for i, pixels in enumerate(frames):
            f = pixels.astype(float)
            rgb = f[..., :3] + 32 * (1 - f[..., 3:4] / 255)
            img = Image.fromarray(np.clip(rgb[..., ::-1], 0, 255).astype(np.uint8))
            sheet.paste(img, (i * renderer.W, 28))
            d.text((i * renderer.W + 20, 8), f"Synthetic reading test / {(1, 2, 4)[i]}x", fill="white")
        sheet.save(output)
    return renderer, frames


class MagnifierTests(unittest.TestCase):
    def test_zoom_limits_and_reset(self):
        app = blob.App.__new__(blob.App)
        app.panel = RecordingPanel(1)
        app.adjust_magnifier_zoom(100)
        self.assertEqual(app.panel.magnifier_zoom, 40)
        app.adjust_magnifier_zoom(-100)
        self.assertEqual(app.panel.magnifier_zoom, 0.25)
        app.adjust_magnifier_zoom()
        self.assertEqual(app.panel.magnifier_zoom, 2)

    def test_capture_preference_restored_after_lens(self):
        app = blob.App.__new__(blob.App)
        app.panel = RecordingPanel(1)
        app.panel.tools_open, app.panel.tool_view = True, "magnifier"
        app.captureable, app.hwnd = True, 123
        app.glass = NS(source=NS(frozen=True), set_magnifier=Mock())
        app.refresh_backdrop = Mock()
        with patch.object(blob.user32, "SetWindowDisplayAffinity", return_value=True) as affinity:
            app._sync_tool_capture()
            self.assertFalse(app.glass.source.frozen)
            affinity.assert_called_with(123, 0x11)
            self.assertTrue(app.captureable)
            app.panel.tools_open = False
            app._sync_tool_capture()
            self.assertTrue(app.glass.source.frozen)
            affinity.assert_called_with(123, 0)
            app.refresh_backdrop.assert_called_once()

    def test_gpu_zoom_changes_source_scale_without_blurring_text(self):
        renderer, frames = render_demo()
        sp = renderer.sp
        crop1 = frames[0][sp+80:sp+176, sp+72:sp+184, :3].astype(float)
        crop2 = frames[1][sp+80:sp+176, sp+72:sp+184, :3].astype(float)
        self.assertGreater(np.mean(np.abs(crop1-crop2)), 20)
        for frame in frames:
            reading = frame[sp+80:sp+176, sp+72:sp+184, :3]
            self.assertLess(np.percentile(reading, 1), 90)
            self.assertGreater(np.percentile(reading, 90), 230)
        # A known grayscale ramp verifies the actual sampling scale, not just
        # a cosmetic change: the slope must halve at 2x and quarter at 4x.
        yy, xx = np.mgrid[:renderer.cap.shape[0], :renderer.cap.shape[1]]
        ramp = np.clip(20 + .35 * xx + .16 * yy, 0, 255).astype(np.uint8)
        renderer.cap[..., :3] = ramp[..., None]
        renderer._bg_dirty = True
        for zoom in (1, 2, 4, 6):
            renderer.set_magnifier((24, 24, 232, 232), zoom)
            with patch.object(glass.user32, "UpdateLayeredWindow", return_value=True):
                renderer.render(None, 0, 0, 324, 324, 1, (-.55, -.83), .9, panel_y=sp)
            pixels = renderer.dib.arr
            difference = float(pixels[sp+128, sp+188, 0]) - float(pixels[sp+128, sp+68, 0])
            self.assertAlmostEqual(difference, 42 / zoom, delta=2)


    def test_circle_and_handle_have_no_rectangular_housing(self):
        renderer, frames = render_demo()
        sp = renderer.sp
        alpha = frames[1][..., 3]
        self.assertEqual(alpha[sp+128, sp+128], 255)
        self.assertEqual(alpha[sp+285, sp+285], 255)
        for x, y in ((20, 20), (280, 80), (80, 280), (310, 210)):
            self.assertLess(alpha[sp+y, sp+x], 30)

    def test_pointer_follows_lens_center_and_restores_blob_position(self):
        app = blob.App.__new__(blob.App)
        app.panel = RecordingPanel(1)
        app.panel.tools_open, app.panel.tool_view = True, "magnifier"
        app.ss, app.snap, app.hwnd = 2, {}, 123
        app.glass = NS(panel_x=lambda width: 420-width, sp=28,
                       panel_y=lambda height: 800-height)
        app.fit_magnifier_in_work_area = Mock()
        app.pos, app.pinned = [0, 0], True
        app._magnifier_home, app._magnifier_follow = ([700, 420], False), True
        with patch.object(blob, "cursor_pos", return_value=(900, 500)):
            app._track_magnifier(324, 324, 24)
        self.assertEqual(app.pos, [676, 348])
        app.fit_magnifier_in_work_area.assert_not_called()
        with patch.object(blob.user32, "GetCapture", return_value=123), \
                patch.object(blob.user32, "ReleaseCapture") as release:
            app._end_magnifier_follow()
            release.assert_called_once()
        self.assertEqual(app.pos, [700, 420])
        self.assertFalse(app.pinned)
        self.assertFalse(app._magnifier_follow)

    def test_right_click_and_capture_loss_disengage_without_dragging(self):
        for message, lp, closes in ((0x204, 0, False), (0x205, 0, True),
                                    (blob.WM_CAPTURECHANGED, 456, True),
                                    (blob.WM_LBUTTONDOWN, 0, False)):
            app = blob.App.__new__(blob.App)
            app._magnifier_follow = True
            app.click = Mock()
            self.assertEqual(app.wndproc(123, message, 0, lp), 0)
            if closes:
                app.click.assert_called_once_with("tool:close", 0)
            else:
                app.click.assert_not_called()

    def test_pointer_is_hidden_while_magnifier_is_active(self):
        app = blob.App.__new__(blob.App)
        app._magnifier_follow = False
        app.panel = NS(magnifier_active=True)
        app.visible = False
        app._overlay_unlocked = True
        app.gaming_modifier_drag = False
        app.panel.page = "blob"
        app.cur_blank = 9876
        with patch.object(blob.user32, "SetCursor") as set_cursor:
            self.assertEqual(app.wndproc(123, blob.WM_SETCURSOR, 0, 0), 1)
        set_cursor.assert_called_once_with(9876)

    def test_renderer_pointer_is_cleared_while_magnifier_is_active(self):
        app = blob.App.__new__(blob.App)
        app.panel = RecordingPanel(1)
        app.panel.tools_open, app.panel.tool_view = True, "magnifier"
        app.springs = blob.Springs()
        app.attached, app.detaching = "old", "older"
        app.glass = NS(set_pointer=Mock())
        app._update_pointer([])
        app.glass.set_pointer.assert_called_once_with(None, 0.0, -1, 0.0)
        self.assertIsNone(app.attached)
        self.assertIsNone(app.detaching)

    def test_palette_latches_and_expires_after_three_seconds(self):
        app = blob.App.__new__(blob.App)
        app.panel = RecordingPanel(1)
        app.panel.page, app.panel.hardware_view = "blob", "bubble"
        app.panel.tool_reveal = 1
        app.panel.draw(*fixtures()[:2], .2, False, media=fixtures()[2])
        app.visible, app._overlay_unlocked, app.mouse_in = True, True, False
        app.ss, app.bubble_linger_until = 2, 0
        app._panel_screen_rect = Mock(return_value=(100, 100, 240, 230))
        app.mouse_xy = (-500, -500)  # stale local coordinates after canvas resize
        with patch.object(blob.time, "perf_counter", return_value=10), \
                patch.object(blob, "cursor_pos", return_value=(279, 155)):
            self.assertEqual(app.bubble_proximity_target(), 1)
            self.assertEqual(app.bubble_linger_until, 13)
        with patch.object(blob, "cursor_pos", return_value=(900, 900)):
            with patch.object(blob.time, "perf_counter", return_value=12.99):
                self.assertEqual(app.bubble_proximity_target(), 1)
            with patch.object(blob.time, "perf_counter", return_value=13.01):
                self.assertEqual(app.bubble_proximity_target(), 0)

    def test_leaving_palette_does_not_force_normal_cards_to_palette_width(self):
        for page in ("blob", "music", "sound", "settings", "gaming"):
            panel = RecordingPanel(1)
            panel.page = page
            panel.tool_reveal = 0
            panel.update_width()
            expected = panel.w
            panel.tool_reveal = 1
            panel.update_width()
            self.assertEqual(panel.w, expected, page)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--preview":
        render_demo(sys.argv[2])
        print(Path(sys.argv[2]).resolve())
    else:
        unittest.main()
