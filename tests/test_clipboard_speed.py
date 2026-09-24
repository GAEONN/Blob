import io
import time
import unittest

from PIL import Image

from test_regressions import RecordingPanel, fixtures
from toolset import ClipboardController


class ClipboardSpeedAndPreviewTests(unittest.TestCase):
    def test_long_text_draws_quickly(self):
        # Trimming one character per measurement made 20k-character items take minutes.
        snap, sound, media, _ = fixtures()
        clip = ClipboardController()
        for i, n in enumerate((300, 20000, 100000)):
            clip.ingest({"kind": "Text", "content": "word " * (n // 5)}, now=i + 1)
        panel = RecordingPanel(1.5)
        panel.tools_open, panel.tool_view, panel.page = True, "clipboard", "blob"
        panel.update_width()
        start = time.perf_counter()
        panel.draw(snap, sound, .35, False, media=media, clipboard=clip)
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_fit_still_ellipsizes_exactly(self):
        panel = RecordingPanel(1)
        text = "x" * 5000
        fitted = panel.fit(text, 13, "Regular", 120)
        self.assertTrue(fitted.endswith("…"))
        font = panel.text_font(fitted, 13, "Regular")
        self.assertLessEqual(font.getlength(fitted), 120)
        self.assertGreater(font.getlength(fitted[:-1] + "x…"), 120)
        self.assertEqual(panel.fit("short", 13, "Regular", 120), "short")

    def test_image_items_get_a_cached_thumbnail(self):
        buf = io.BytesIO()
        Image.new("RGB", (640, 320), (200, 40, 40)).save(buf, "BMP")
        clip = ClipboardController()
        clip.ingest({"kind": "Image", "content": buf.getvalue()[14:], "format": 8}, now=1)
        item = clip.items[0]
        thumb = clip.thumbnail(item)
        self.assertEqual(thumb.size, (256, 128))
        self.assertEqual(thumb.getpixel((10, 10))[:3], (200, 40, 40))
        self.assertIs(clip.thumbnail(item), thumb)
        clip.ingest({"kind": "Text", "content": "hello"}, now=2)
        self.assertIsNone(clip.thumbnail(clip.items[0]))


if __name__ == "__main__":
    unittest.main()
