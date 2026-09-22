"""Clipboard card behaviour, fit rules and offscreen glass rendering."""
import struct
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageDraw

from test_regressions import RecordingPanel, blob, fixtures, glass
from toolset import ClipboardController


def make_clipboard():
    clipboard = ClipboardController()
    clipboard.ingest({"kind": "Text", "content": "Meeting notes\nBring the screenshots and review the export."}, now=1)
    clipboard.ingest({"kind": "Files", "content": [r"C:\\work\\frame-01.png", r"C:\\work\\frame-02.png"]}, now=2)
    dib = struct.pack("<Iii", 40, 1920, -1080) + b"\0" * 96
    clipboard.ingest({"kind": "Image", "content": dib, "format": clipboard.CF_DIBV5,
                      "dimensions": (1920, 1080), "byte_size": len(dib)}, now=3)
    for index in range(6):
        clipboard.ingest({"kind": "Text", "content": f"Clipboard archive item {index}"}, now=4 + index)
    return clipboard


class ClipboardControllerTests(unittest.TestCase):
    def test_history_is_session_only_deduped_and_replayable(self):
        clipboard = ClipboardController()
        self.assertTrue(clipboard.ingest({"kind": "Text", "content": "first"}, now=1))
        text_id = clipboard.selected_id
        self.assertTrue(clipboard.ingest({"kind": "Files", "content": [r"C:\\work\\one.png", r"C:\\work\\two.png"]}, now=2))
        files_id = clipboard.selected_id
        dib = struct.pack("<Iii", 40, 800, -600) + b"\0" * 48
        self.assertTrue(clipboard.ingest({"kind": "Image", "content": dib, "format": clipboard.CF_DIBV5}, now=3))
        image_id = clipboard.selected_id
        self.assertEqual(clipboard.item(image_id)["dimensions"], (800, 600))
        self.assertTrue(clipboard.can_copy(clipboard.item(image_id)))

        # A repeated clipboard event promotes its session item without creating noise.
        self.assertFalse(clipboard.ingest({"kind": "Text", "content": "first"}, now=4))
        self.assertEqual(len(clipboard.items), 3)
        self.assertEqual(clipboard.selected_id, text_id)

        with patch.object(clipboard, "_write_text", return_value=True) as write_text:
            self.assertTrue(clipboard.copy(text_id))
        write_text.assert_called_once_with("first")
        with patch.object(clipboard, "_write_files", return_value=True) as write_files:
            self.assertTrue(clipboard.copy(files_id))
        write_files.assert_called_once_with([r"C:\\work\\one.png", r"C:\\work\\two.png"])
        with patch.object(clipboard, "_write_payload", return_value=True) as write_image:
            self.assertTrue(clipboard.copy(image_id))
        write_image.assert_called_once_with(clipboard.CF_DIBV5, dib)

    def test_pinned_tab_pagination_and_live_images_are_honest(self):
        clipboard = make_clipboard()
        self.assertEqual(clipboard.page_count(), 2)
        first_page = clipboard.page_items()
        clipboard.move_page(1)
        self.assertEqual(len(clipboard.page_items()), 4)
        clipboard.select(first_page[-1]["id"])
        clipboard.toggle_pin(first_page[-1]["id"])
        clipboard.set_tab("pinned")
        self.assertEqual(len(clipboard.page_items()), 1)
        self.assertTrue(clipboard.item(visible_only=True)["pinned"])
        clipboard.set_tab("recent")
        self.assertEqual(clipboard.clear_unpinned(), 8)
        self.assertEqual(len(clipboard.items), 1)

        live = ClipboardController()
        live.ingest({"kind": "Image", "content": b"", "live_only": True, "byte_size": 96 * 1024 * 1024})
        self.assertFalse(live.can_copy(live.item()))
        self.assertFalse(live.copy())
        self.assertIn("too large", live.status)

    def test_multi_select_restores_real_file_sets_and_rejects_mixed_payloads(self):
        clipboard = ClipboardController()
        clipboard.ingest({"kind": "Files", "content": [r"C:\\shots\\one.png"]})
        first = clipboard.selected_id
        clipboard.ingest({"kind": "Files", "content": [r"C:\\shots\\two.png", r"C:\\shots\\three.png"]})
        second = clipboard.selected_id
        clipboard.select(first)
        clipboard.toggle_selection(second)
        with patch.object(clipboard, "_write_files", return_value=True) as write_files:
            self.assertTrue(clipboard.copy_selected())
        write_files.assert_called_once_with([r"C:\\shots\\two.png", r"C:\\shots\\three.png", r"C:\\shots\\one.png"])

        clipboard.ingest({"kind": "Text", "content": "not a file"})
        clipboard.toggle_selection(first)
        self.assertFalse(clipboard.copy_selected())
        self.assertIn("one image", clipboard.status)


class ClipboardLayoutTests(unittest.TestCase):
    def assert_bounds(self, panel):
        for label, bounds in list(panel.labels) + list(panel.rects.items()):
            self.assertGreaterEqual(bounds[0], 0, (label, bounds))
            self.assertGreaterEqual(bounds[1], 0, (label, bounds))
            self.assertLessEqual(bounds[2], panel.w, (label, bounds, panel.w))
            self.assertLessEqual(bounds[3], panel.ink.height, (label, bounds, panel.ink.height))

    def test_clipboard_uses_the_calculator_card_contract_at_every_dpi(self):
        clipboard = make_clipboard()
        snap, sound, media, _ = fixtures()
        for dpi in (1, 1.25, 1.5, 2):
            for scale in (1, .8):
                with self.subTest(dpi=dpi, scale=scale):
                    panel = RecordingPanel(dpi)
                    panel.tools_open, panel.tool_view, panel.tool_size_scale = True, "clipboard", scale
                    panel.draw(snap, sound, .32, False, media=media, clipboard=clipboard)
                    self.assertEqual(panel.w, round(panel.TOOL_W * panel.S * scale))
                    self.assertEqual(panel.height(snap), round(panel.TOOL_H * panel.S * scale))
                    self.assertIn("clip:refresh", panel.rects)
                    self.assertIn("cliptab:recent", panel.rects)
                    self.assertIn("cliptab:pinned", panel.rects)
                    self.assertNotIn("tool:new", panel.rects)
                    self.assert_bounds(panel)

        image = next(item for item in clipboard.items if item["kind"] == "Image")
        clipboard.select(image["id"])
        panel = RecordingPanel(1)
        panel.tools_open, panel.tool_view = True, "clipboard"
        panel.draw(snap, sound, .32, False, media=media, clipboard=clipboard)
        self.assertTrue(any("1,920 × 1,080" in text for text, _ in panel.labels))

    def test_card_actions_rewire_state_without_rebuilding_the_window(self):
        clipboard = make_clipboard()
        app = blob.App.__new__(blob.App)
        app.clipboard, app.panel = clipboard, NS()
        app.draw_content, app.frame = Mock(), Mock()
        ident = clipboard.items[0]["id"]
        app.click("clip:pin:" + ident, 0)
        self.assertTrue(clipboard.item(ident)["pinned"])
        app.click("cliptab:pinned", 0)
        self.assertEqual(clipboard.tab, "pinned")
        app.click("clip:clear", 0)
        self.assertEqual(len(clipboard.items), 1)
        self.assertTrue(app.draw_content.called)
        self.assertTrue(app.frame.called)

    def test_clipboard_card_clamps_inside_positive_and_negative_work_areas(self):
        for dpi in (1, 1.5, 2):
            for work in (NS(left=0, top=0, right=1280, bottom=680),
                         NS(left=-1920, top=-400, right=0, bottom=600)):
                with self.subTest(dpi=dpi, work=work):
                    app = blob.App.__new__(blob.App)
                    app.panel = RecordingPanel(dpi)
                    app.panel.tools_open, app.panel.tool_view = True, "clipboard"
                    app.S, app.ss, app.snap = dpi, 2, {}
                    app.pos, app.tool_top = [work.right - 70, work.bottom - 100], 240 * dpi
                    app.glass = NS(sp=28 * dpi, H=900 * dpi, W=680 * dpi,
                                   panel_x=lambda width: 660 * dpi - width)
                    app.springs = blob.Springs()
                    app.springs.get("width", app.panel.w)
                    app.springs.get("height", app.panel.height({}))
                    app._panel_screen_rect = lambda: (work.right - 90, work.bottom - 90, 80, 80)
                    with patch.object(blob, "work_area_at", return_value=(work, None)):
                        app.fit_calculator_in_work_area()
                    app.springs["width"].x = app.panel.w
                    app.springs["height"].x = app.panel.height({}) * 1.03
                    app._constrain_calculator_frame()
                    width, height = app.springs["width"].x / 2, app.springs["height"].x / 2
                    x, y = app.pos[0] + app.glass.panel_x(width), app.pos[1] + app.tool_top
                    self.assertGreaterEqual(x - app.glass.sp, work.left - .01)
                    self.assertGreaterEqual(y - app.glass.sp, work.top - .01)
                    self.assertLessEqual(x + width + app.glass.sp, work.right + .01)
                    self.assertLessEqual(y + height + app.glass.sp, work.bottom + .01)

    def test_real_glass_preview_keeps_every_clipboard_control_inside_the_card(self):
        renderer, frames, counts = render_clipboard()
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(count <= glass.MAX_LENSES for count in counts))
        self.assertGreater(counts[0], 10)
        # The synthetic bright/dark desktop must visibly refract through the card.
        frame = frames[0]
        self.assertGreater(float(frame[..., 3].mean()), 18)


def render_clipboard(output=None):
    snap, sound, media, _ = fixtures()
    with patch.object(glass, "ScreenSource", return_value=NS(frozen=False)):
        renderer = glass.GlassRenderer(1, 420, 748)
    renderer.set_supersample(2)
    renderer.set_tool_card(True)
    hh, ww = renderer.cap.shape[:2]
    yy, xx = np.mgrid[:hh, :ww]
    base = np.where(xx < ww * .52, 34 + yy * .035, 236 - yy * .025).astype(np.uint8)
    renderer.cap[..., :3] = base[..., None]
    renderer.cap[..., 3] = 255
    clipboard = make_clipboard()
    clipboard.toggle_pin(clipboard.items[2]["id"])
    variants = (("recent", 0), ("pinned", 0))
    frames, counts = [], []
    for tab, page in variants:
        clipboard.set_tab(tab)
        clipboard.page = page
        panel = RecordingPanel(1)
        panel.tools_open, panel.tool_view = True, "clipboard"
        panel.draw(snap, sound, .34, False, media=media, clipboard=clipboard)
        app = blob.App.__new__(blob.App)
        app.panel, app.S, app.springs = panel, 1, blob.Springs()
        app.attached = app.detaching = app.hover = app.pressed = app.slider_drag = None
        app.pointer_style = "system"
        lenses, _ = app.resolve(panel.controls, 1, "")
        counts.append(len(lenses))
        for lens in lenses:
            lens["rect"] = tuple(value / panel.SS for value in lens["rect"])
            for key in ("r", "bevel", "strength"):
                if key in lens:
                    lens[key] /= panel.SS
        renderer.set_lenses(lenses)
        renderer.set_content(panel.ink, panel.accent, panel.pic, instant=True)
        with patch.object(glass.user32, "UpdateLayeredWindow", return_value=True):
            renderer.render(None, 0, 0, 420, panel.height(snap) / panel.SS, 1,
                            (-.55, -.83), .35, panel_y=renderer.sp)
        frames.append(renderer.dib.arr.copy())
    if output:
        sheet = Image.new("RGB", (renderer.W * 2, renderer.H + 28), (30, 30, 30))
        draw = ImageDraw.Draw(sheet)
        for index, pixels in enumerate(frames):
            rgb = pixels[..., :3].astype(float) + 30 * (1 - pixels[..., 3:4] / 255)
            image = Image.fromarray(np.clip(rgb[..., ::-1], 0, 255).astype(np.uint8))
            x = index * renderer.W
            draw.text((x + 16, 8), f"Clipboard / {variants[index][0]} / synthetic desktop", fill="white")
            sheet.paste(image, (x, 28))
        sheet.save(output)
    return renderer, frames, counts


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--preview":
        render_clipboard(sys.argv[2])
    else:
        unittest.main()
