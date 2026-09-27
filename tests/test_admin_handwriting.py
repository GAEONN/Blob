"""Local handwriting profile workflow and UI regressions; all samples are synthetic."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import admin_handwriting
import handwriting_generate
import handwriting_profile as profile
import test_regressions as reg


def wait_for(condition, timeout=30):
    end = time.time() + timeout
    while not condition() and time.time() < end:
        time.sleep(.02)
    if not condition():
        raise AssertionError("Background handwriting task did not finish")


def write_synthetic_sheet(page_id, path):
    """Make a complete fixture from repeated geometric marks, not personal writing."""
    image = Image.new("L", profile.SHEET_SIZE, 255)
    draw = ImageDraw.Draw(image)
    w, h = profile.SHEET_SIZE
    inset, size = 82, 82
    for x, y in ((inset, inset), (w-inset-size, inset),
                 (w-inset-size, h-inset-size), (inset, h-inset-size)):
        draw.rectangle((x, y, x+size, y+size), fill=15)
    for cell in profile.sheet_layout(page_id):
        for (rect, baseline) in cell["samples"]:
            x0, y0, _, y1 = rect
            x = x0 + 12
            height = round((y1-y0) * .56)
            top = baseline - height
            width = max(5, round(height * .18))
            draw.ellipse((x, top, x + height*.88, baseline - 3), outline=18, width=width)
            draw.line((x + height*.86, top + height*.44, x + height*.86, baseline - 1),
                      fill=18, width=width)
    image.save(path, format="PNG", optimize=True)


class HandwritingControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.downloads = self.root / "Downloads"
        self.downloads.mkdir()
        self.hw = admin_handwriting.HandwritingTool(
            clipboard=lambda: "A local clipboard example.", downloads=self.downloads,
            profile_home=self.root / "local-profile", work=self.root / "work",
            out_root=self.root / "output")

    def tearDown(self):
        self.temp.cleanup()

    def test_private_profile_is_built_from_six_samples_and_sources_are_not_copied(self):
        source_folder = self.downloads / "private scans"
        source_folder.mkdir()
        for page_id in admin_handwriting.PROFILE_PAGES:
            source = source_folder / f"{page_id}.png"
            write_synthetic_sheet(page_id, source)
            self.assertTrue(self.hw.attach_profile_photo(page_id, source))

        self.assertTrue(self.hw.build_profile())
        wait_for(lambda: not self.hw.profile_building)
        self.assertIsNone(self.hw.error)
        self.assertTrue(self.hw.profile_ready)
        self.assertEqual(self.hw.section, "write")
        self.assertEqual(self.hw.profile_photos, {})

        local_profile = profile.load_profile(self.hw.profile_home)
        self.assertIsNotNone(local_profile)
        self.assertEqual(local_profile["manifest"]["samples_per_character"], 6)
        expected = {char for _, chars in profile.PAGE_SPECS.values() for char in chars}
        self.assertEqual(set(local_profile["glyphs"]), expected)
        self.assertTrue(all(len(variants) == 6 for variants in local_profile["glyphs"].values()))
        manifest_text = (local_profile["directory"] / "profile.json").read_text(encoding="utf-8")
        for page_id in admin_handwriting.PROFILE_PAGES:
            self.assertNotIn(f"{page_id}.png", manifest_text)
            self.assertFalse((local_profile["directory"] / f"{page_id}.png").exists())
        self.assertTrue(all(path.exists() for path in source_folder.iterdir()))

        source = self.root / "sample.txt"
        source.write_text("hello blob 123", encoding="utf-8")
        output = handwriting_generate.render_document(
            source, self.hw.profile_home, title="hello", output_root=self.root / "written", seed=4)
        self.assertTrue(Path(output["pdf"]).is_file())

    def test_profile_refuses_a_character_with_fewer_than_six_clear_samples(self):
        source_folder = self.downloads / "partial scans"
        source_folder.mkdir()
        for page_id in admin_handwriting.PROFILE_PAGES:
            source = source_folder / f"{page_id}.png"
            write_synthetic_sheet(page_id, source)
            if page_id == "lower":
                cells = profile.sheet_layout(page_id)
                rect, _ = cells[0]["samples"][0]
                with Image.open(source) as image:
                    ImageDraw.Draw(image).rectangle(rect, fill=255)
                    image.save(source)
            self.assertTrue(self.hw.attach_profile_photo(page_id, source))
        self.assertTrue(self.hw.build_profile())
        wait_for(lambda: not self.hw.profile_building)
        self.assertIn("six clear samples", self.hw.error)
        self.assertFalse((self.hw.profile_home / "active.json").exists())

    def test_sheet_geometry_has_exactly_six_writing_lanes_for_every_character(self):
        for page_id, (_, chars) in profile.PAGE_SPECS.items():
            cells = profile.sheet_layout(page_id)
            self.assertEqual([cell["char"] for cell in cells], list(chars))
            self.assertTrue(all(len(cell["samples"]) == 6 for cell in cells))

    def test_printable_worksheet_pdf_is_created_locally(self):
        target = profile.create_training_pdf(self.root / "private-profile")
        self.assertTrue(target.is_file())
        self.assertGreater(target.stat().st_size, 10_000)
        self.assertTrue(target.read_bytes().startswith(b"%PDF-"))

    def test_download_photo_picker_is_filtered_and_stays_inside_downloads(self):
        photo = self.downloads / "lower.png"
        Image.new("RGB", (32, 32), "white").save(photo)
        (self.downloads / "task.docx").write_bytes(b"not a photo")
        self.assertTrue(self.hw.pick_profile_photo("lower"))
        wait_for(lambda: not self.hw.browse_loading)
        self.assertEqual(self.hw.browse_filter, "photo")
        self.assertEqual([entry[0].name for entry in self.hw.browse_entries], ["lower.png"])
        self.assertTrue(self.hw.open_browse_entry(0))
        self.assertEqual(self.hw.profile_photos["lower"], photo)
        self.assertFalse(self.hw.picker_open)
        self.assertFalse(self.hw.browse_to(self.root))
        self.assertIn("inside Downloads", self.hw.browse_error)

    def test_task_files_and_clipboard_stay_in_local_workflow(self):
        self.assertTrue(self.hw.use_clipboard())
        wait_for(lambda: "Checking" not in self.hw.status)
        self.assertEqual(self.hw.source.read_text(encoding="utf-8"), "A local clipboard example.")
        self.assertFalse(self.hw.ready)  # a profile is required before rendering
        unsupported = self.root / "image.png"
        Image.new("RGB", (5, 5)).save(unsupported)
        self.assertFalse(self.hw.use_file(unsupported))
        self.assertIn("Unsupported", self.hw.error)


class HandwritingLayoutTests(unittest.TestCase):
    def setUp(self):
        self.snap, self.sound, self.media, _ = reg.fixtures()
        self.temp = tempfile.TemporaryDirectory()
        self.downloads = Path(self.temp.name) / "Downloads"
        self.downloads.mkdir()
        self.hw = admin_handwriting.HandwritingTool(
            downloads=self.downloads, profile_home=Path(self.temp.name) / "profile")

    def tearDown(self):
        self.temp.cleanup()

    def draw_tool(self, scale=1, size=1, section="profile", picker=False):
        panel = reg.RecordingPanel(scale)
        panel.tool_view = "handwriting"
        panel.tools_open = True
        panel.tool_size_scale = size
        panel.handwriting = self.hw
        self.hw.section = section
        self.hw.picker_open = picker
        self.hw.browse_loading = False
        if picker:
            self.hw.browse_filter = "photo"
            self.hw.browse_dir = self.downloads
            self.hw.browse_entries = [
                (self.downloads / f"Sample {index:02}.jpg", False) for index in range(8)
            ]
        panel.draw(self.snap, self.sound, .35, False, media=self.media)
        return panel

    def assert_inside(self, panel):
        for text, bounds in panel.labels:
            if text:
                self.assertGreaterEqual(bounds[0], 0, (text, bounds))
                self.assertGreaterEqual(bounds[1], 0, (text, bounds))
                self.assertLessEqual(bounds[2], panel.w, (text, bounds, panel.w))
                self.assertLessEqual(bounds[3], panel.ink.height, (text, bounds, panel.ink.height))
        for key, bounds in panel.rects.items():
            self.assertGreaterEqual(bounds[0], 0, (key, bounds))
            self.assertGreaterEqual(bounds[1], 0, (key, bounds))
            self.assertLessEqual(bounds[2], panel.w, (key, bounds, panel.w))
            self.assertLessEqual(bounds[3], panel.ink.height, (key, bounds, panel.ink.height))

    def test_profile_setup_and_writer_fit_across_display_scales(self):
        self.assertEqual(reg.blob.Panel.HANDWRITING_W, reg.blob.Panel.TOOL_W)
        self.assertEqual(reg.blob.Panel.HANDWRITING_H, reg.blob.Panel.TOOL_H)
        for dpi in (1, 1.25, 1.5, 2):
            for size in (.75, 1):
                with self.subTest(dpi=dpi, size=size, section="profile"):
                    panel = self.draw_tool(dpi, size, "profile")
                    self.assertEqual(panel.w, round(panel.TOOL_W * panel.S * size))
                    self.assertEqual(panel.height(self.snap), round(panel.TOOL_H * panel.S * size))
                    self.assertTrue({"hw:sheets:make", "hw:photo:lower", "hw:photo:upper",
                                     "hw:photo:marks", "hw:section:write"} <= panel.rects.keys())
                    self.assert_inside(panel)

                self.hw.profile_info = {"characters": 100, "variants": 600}
                self.hw.section = "write"
                self.hw.source = self.downloads / "Essay.txt"
                self.hw.source_label = "Essay.txt"
                self.hw.source_meta = "Text · TXT"
                self.hw.status = "Ready · preview or create your PDF"
                self.hw.error = None
                with self.subTest(dpi=dpi, size=size, section="write"):
                    panel = self.draw_tool(dpi, size, "write")
                    self.assertTrue({"hw:clipboard", "hw:file", "hw:mode:texto", "hw:ink:negra"}
                                    <= panel.rects.keys())
                    self.assert_inside(panel)
                self.hw.profile_info = None
                self.hw.source = None

    def test_photo_picker_rows_do_not_overlap_header_and_fit(self):
        panel = self.draw_tool(picker=True)
        header = [panel.rects[key] for key in ("hw:picker:back", "tool:minimize", "tool:close")]
        rows = [box for key, box in panel.rects.items() if key.startswith("hw:entry:")]
        self.assertEqual(len(rows), 8)
        for row in rows:
            for control in header:
                self.assertFalse(reg.intersects(row, control), (row, control))
        self.assert_inside(panel)


if __name__ == "__main__":
    unittest.main()
