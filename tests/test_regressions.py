"""Offline regressions: no audio routing, sensor polling, or live desktop capture.

Run on Windows with: python -m unittest discover -s tests -v
"""
import asyncio
import importlib.machinery
import importlib.util
from pathlib import Path
import queue
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
loader = importlib.machinery.SourceFileLoader("blob_layout", str(ROOT / "blob.pyw"))
spec = importlib.util.spec_from_loader(loader.name, loader)
blob = importlib.util.module_from_spec(spec)
loader.exec_module(blob)
import glass
import sound
from media import NowPlaying
from toolset import ToolController


def fixtures():
    snap = dict(cpu=dict(temp=86, load=42, clock=3800),
                gpu=dict(temp=74, load=35, power=45, state="active"),
                fans=[dict(rpm=4300), dict(rpm=4100)],
                control=dict(mode="auto", active="balanced", reason="CPU load is stable"),
                caps=dict(nvidia=True, fans_readable=True), cpu_source="hwmonitor",
                sensors=[dict(name="Very long motherboard temperature sensor label " + str(i),
                              value=42, unit="°C", temp=True) for i in range(40)],
                power=dict(battery=81, plugged=True))
    sound = NS(enabled=True, boost=.5, boost_db=7.5, bass=.3, clarity=.3, surround=.25,
               preset="music", output="Speakers (Very Long USB Audio Device Name)",
               outputs=["Speakers (Very Long USB Audio Device Name)", "Odyssey G40B (NVIDIA High Definition Audio)"],
               cable=True, fx_conflict=False, error="", volume=.65, spectrum=np.zeros(28))
    media = NS(active=True, title="A very long song title to test clipping", artist="An artist",
               album="An album", source="Apple Music", playing=True, duration=300,
               pos_now=lambda: 42, art=Image.new("RGB", (256, 256), (165, 75, 42)))
    item = dict(title=media.title, artist=media.artist, album=media.album, art=media.art)
    am = NS(results=[dict(item) for _ in range(30)], queue=[dict(item) for _ in range(30)],
            status="A very long playback status that must not escape the window or cover buttons",
            shuffle=True, repeat="all")
    return snap, sound, media, am


class RecordingPanel(blob.Panel):
    def _begin(self, height):
        super()._begin(height)
        self.labels = []

    def label(self, x, y, t, size, style="Regular", a=175, anchor="la", temp=None, icon=False):
        font = self.f.icon(size) if icon else self.text_font(t, size, style)
        bounds = self.di.textbbox((x, y), t, font=font, anchor=anchor)
        self.labels.append((t, bounds))
        super().label(x, y, t, size, style, a, anchor, temp, icon)


def intersects(a, b):
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.snap, self.sound, self.media, self.am = fixtures()

    def panel(self, scale=1, compact=False, page="blob", view="now"):
        p = RecordingPanel(scale)
        p.set_compact(compact)
        p.page, p.music_view, p.am = page, view, self.am
        p.hw_note = "CPU, GPU and fan sensors come from LibreHardwareMonitor."
        return p

    def draw(self, p, **kwargs):
        p.draw(self.snap, self.sound, .35, False, media=self.media, **kwargs)

    def assert_bounds(self, p):
        for text, box in p.labels:
            if not text:
                continue
            self.assertGreaterEqual(box[0], 0, (text, box))
            self.assertLessEqual(box[2], p.w, (text, box, p.w))
            self.assertGreaterEqual(box[1], 0, (text, box))
            self.assertLessEqual(box[3], p.ink.height, (text, box))
        for key, box in p.rects.items():
            self.assertGreaterEqual(box[0], 0, (key, box))
            self.assertLessEqual(box[2], p.w, (key, box))
            self.assertGreaterEqual(box[1], 0, (key, box))
            self.assertLessEqual(box[3], p.ink.height, (key, box))

    def test_all_pages_and_dpi(self):
        for dpi in (1, 1.25, 1.5, 2):
            for compact in (False, True):
                for page, view in (("blob", "now"), ("sound", "now"), ("settings", "now"),
                                   ("music", "now"), ("music", "art"),
                                   ("music", "queue"), ("music", "search"),
                                   ("music", "bubble")):
                    with self.subTest(dpi=dpi, compact=compact, page=page, view=view):
                        p = self.panel(dpi, compact, page, view)
                        p.tabs_t = 1
                        self.draw(p)
                        self.assert_bounds(p)

    def test_smallblob_gaming_strip_orientations_fit_at_every_dpi(self):
        for dpi in (1, 1.25, 1.5, 2):
            for compact in (False, True):
                for view in ("horizontal", "vertical"):
                    with self.subTest(dpi=dpi, compact=compact, view=view):
                        p = self.panel(dpi, compact, page="gaming")
                        p.gaming_view = view
                        p.tabs_t = 1
                        self.draw(p)
                        self.assert_bounds(p)
                        expected = ((p.GAMING_VERTICAL_WIDE, p.GAMING_VERTICAL_NARROW)
                                    if view == "vertical" else (p.GAMING_WIDE, p.GAMING_NARROW))
                        self.assertEqual(p.w, expected[bool(compact)] * p.S)
                        self.assertFalse(any(k.startswith("size:") for k in p.rects))
                        self.assertNotIn("gview:vertical", p.rects)
                        self.assertNotIn("gview:horizontal", p.rects)
                        self.assertIn("gview:bubble", p.rects)

    def test_tool_headers_mirror_the_minimize_close_rail_with_monitor_side(self):
        with tempfile.TemporaryDirectory() as folder:
            tools = ToolController(Path(folder) / "tools.json")
            for side in ("left", "right"):
                with self.subTest(side=side):
                    p = self.panel(page="blob")
                    p.tools_open, p.tool_view, p.anchor_side = True, "calculator", side
                    p.update_width()
                    self.draw(p, tools=tools)
                    minimize = p.rects["tool:minimize"]
                    close = p.rects["tool:close"]
                    if side == "left":
                        self.assertLess((minimize[0] + minimize[2]) / 2, p.w / 2)
                        self.assertLess((close[0] + close[2]) / 2, p.w / 2)
                    else:
                        self.assertGreater((minimize[0] + minimize[2]) / 2, p.w / 2)
                        self.assertGreater((close[0] + close[2]) / 2, p.w / 2)
                    self.assert_bounds(p)

    def test_gaming_compact_is_independent_from_orientation_and_menu_side(self):
        self.assertEqual(blob.normalize_gaming_view("bubble"), "bubble")
        for view in ("horizontal", "vertical"):
            for side in ("left", "right"):
                with self.subTest(view=view, side=side):
                    p = self.panel(compact=True, page="gaming")
                    p.gaming_view = view
                    p.tabs_side = side
                    p.tabs_open = True
                    p.tabs_t = 1
                    self.draw(p)
                    self.assertEqual(p.gaming_view, view)
                    self.assertIn("page:gaming", p.rects)
                    self.assert_bounds(p)

    def test_horizontal_metrics_clear_both_control_rails(self):
        p = self.panel(compact=False, page="gaming")
        p.game = dict(fps=144, frame_ms=6.94, ram_percent=62, ram_gb=19.8)
        fps_boxes = {}
        for side in ("left", "right"):
            p.tabs_side = side
            self.draw(p)
            fps_boxes[side] = next(bounds for text, bounds in p.labels if text == "FPS")
            self.assertFalse(intersects(fps_boxes[side], p.rects["gview:bubble"]))
            self.assertFalse(intersects(fps_boxes[side], p.rects["tabs"]))
        self.assertEqual(fps_boxes["left"], fps_boxes["right"])

    def test_settings_labels_clear_toggles_and_all_controls_reachable(self):
        expected = {"backdrop", "queueart", "musicreactive", "soundstart", "startup", "capture"}
        for compact in (False, True):
            p, seen = self.panel(compact=compact, page="settings"), set()
            for row in range(20):
                p.scroll["settings"] = row
                self.draw(p)
                self.assert_bounds(p)
                for key, box in p.rects.items():
                    if key.startswith("toggle:"):
                        seen.add(key.split(":")[1])
                        for text, bounds in p.labels:
                            self.assertFalse(intersects(bounds, box), (compact, row, key, text))
                    if key != "tabs":
                        self.assertLessEqual(box[3], p.ink.height - 56 * p.S)
            self.assertEqual(seen, expected)

    def test_music_restores_distinct_regular_and_compact_sizes(self):
        sizes = set()
        for compact in (False, True):
            p = self.panel(compact=compact, page="music", view="now")
            p.tabs_t = 0
            self.draw(p)
            sizes.add((p.w, p.height(self.snap)))
            self.assertFalse(any(k.startswith("size:") for k in p.rects))
        self.assertEqual(len(sizes), 2)

        p = self.panel(page="music", view="art")
        self.draw(p)
        self.assertEqual(p.height(self.snap), p.w)
        self.assertNotIn("tabs", p.rects)

    def test_music_card_bubble_affordance_clears_art_and_seek(self):
        p = self.panel(page="music", view="now")
        self.draw(p)
        self.assertIn("mview:bubble", p.rects)
        bubble = p.rects["mview:bubble"]
        for key in ("mview:art", "slider:seek"):
            self.assertIn(key, p.rects)
            self.assertFalse(intersects(bubble, p.rects[key]), key)
        self.assert_bounds(p)

    def test_music_card_and_cover_expose_volume_and_options(self):
        for view in ("art",):
            with self.subTest(view=view):
                p = self.panel(page="music", view=view)
                if view == "art":
                    p.hover_key = "arthover"
                self.draw(p)
                self.assertIn("musicutil:volume", p.rects)
                self.assertIn("musicutil:options", p.rects)

                p.music_menu = "volume"
                self.draw(p)
                self.assertIn("slider:volume", p.rects)
                self.assertNotIn("media:toggle", p.rects)
                self.assertIn("musicutil:volume", p.rects)

                p.music_menu = "options"
                self.draw(p)
                self.assertNotIn("media:toggle", p.rects)
                for key in ("mview:search", "mview:queue", "toggle:musicreactive",
                            "am:shuffle", "am:repeat"):
                    self.assertIn(key, p.rects)
                self.assertIn("musicutil:options", p.rects)
                self.assert_bounds(p)

    def test_music_bubble_has_gesture_body_and_hover_restore_satellite(self):
        p = self.panel(page="music", view="bubble")
        self.draw(p)
        self.assertEqual(p.w, p.BUBBLE_W * p.S)
        self.assertEqual(p.height(self.snap), p.BUBBLE_H * p.S)
        self.assertIn("mbubble:gesture", p.rects)
        self.assertNotIn("mview:now", p.rects)
        self.assertNotIn("tabs", p.rects)
        # The restore satellite belongs to the hover reveal and is absent after
        # the linger has settled back to the quiet circular state.
        p.tool_reveal = 1.0
        self.draw(p)
        self.assertEqual(p.w, p.TOOL_PALETTE_W * p.S)
        self.assertIn("mview:now", p.rects)
        self.assertEqual(p.hit(p.bubble_x(82.5), 22.5 * p.S), "mview:now")
        self.assertEqual(p.hit(p.bubble_x(43), 62 * p.S), "mbubble:gesture")
        self.assert_bounds(p)

    def test_system_bubble_shows_cpu_gpu_temperatures_and_hover_restore_satellite(self):
        p = self.panel(page="blob")
        p.hardware_view = "bubble"
        self.draw(p)
        labels = [text for text, _ in p.labels]
        self.assertIn("CPU", labels)
        self.assertIn("GPU", labels)
        self.assertIn("86°", labels)
        self.assertIn("74°", labels)
        self.assertNotIn("hview:card", p.rects)
        p.tool_reveal = 1.0
        self.draw(p)
        self.assertIn("hview:card", p.rects)
        self.assertEqual(p.hit(p.bubble_x(82.5), 22.5 * p.S), "hview:card")
        self.assert_bounds(p)

    def test_tool_satellites_keep_the_main_bubble_and_expand_readably(self):
        p = self.panel(page="blob")
        p.hardware_view = "bubble"
        p.tool_reveal = 1.0
        self.draw(p)
        labels = [text for text, _ in p.labels]
        self.assertIn("CPU", labels)
        self.assertIn("GPU", labels)
        self.assertIn("tool:calculator", p.rects)
        self.assertIn("tool:clipboard", p.rects)
        self.assertIn("tool:blank", p.rects)
        self.assertNotIn("∑", labels)
        self.assertGreaterEqual(p.rects["tool:calculator"][2] - p.rects["tool:calculator"][0], 24 * p.S)
        self.assertEqual(p.w, p.TOOL_PALETTE_W * p.S)
        self.assertEqual(p.height(self.snap), p.TOOL_PALETTE_H * p.S)
        self.assertTrue(any(c[0] == "static" for c in p.controls))
        self.assert_bounds(p)

    def test_blank_tool_slot_opens_a_monitor_safe_empty_canvas(self):
        for scale in (1, 1.25, 1.5, 2):
            with self.subTest(scale=scale):
                p = self.panel(scale=scale, page="blob")
                p.tools_open, p.tool_view = True, "blank"
                self.draw(p)
                self.assertEqual(p.w, p.TOOL_W * p.S)
                self.assertEqual(p.height(self.snap), p.TOOL_H * p.S)
                self.assertIn("tool:minimize", p.rects)
                self.assertIn("tool:close", p.rects)
                self.assertIn("New tool", [text for text, _ in p.labels])
                self.assert_bounds(p)

    def test_radial_palette_hit_testing_preserves_gaps_and_bubble(self):
        import math
        for side in ("left", "right"):
            for scale in (1, 1.5, 2):
                p = self.panel(scale=scale, page="blob")
                p.hardware_view, p.anchor_side, p.tool_reveal = "bubble", side, 1.0
                self.draw(p)
                self.assert_bounds(p)
                self.assertEqual(len(p.tool_arcs), 4)
                angles = []
                for key, (cx, cy, orbit, thickness, angle, half_angle) in p.tool_arcs.items():
                    self.assertEqual(p.hit(cx + orbit * math.cos(angle),
                                           cy + orbit * math.sin(angle)), key)
                    self.assertEqual(cx, p.bubble_x(43))
                    angles.append(angle)
                for first, second in zip(angles, angles[1:]):
                    middle = (first + second) / 2
                    self.assertIsNone(p.hit(cx + orbit * math.cos(middle),
                                             cy + orbit * math.sin(middle)))
                p.tool_reveal = 0.0
                self.draw(p)
                self.assertFalse(p.tool_arcs)
                self.assertNotIn("hview:card", p.rects)
                self.assertIn("hcycle", p.rects)

    def test_palette_unions_lobes_as_one_smooth_surface(self):
        """The glass shader must not reintroduce cusp seams between lobes."""
        self.assertIn("palette = smin(palette, lensSd(i, p), 18.0*S)", glass.FRAG)
        self.assertNotIn("shape = min(shape, smin(body, lensSd(i, p), 18.0*S))", glass.FRAG)

    def test_calculator_card_uses_readable_scientific_and_numeric_grids(self):
        with tempfile.TemporaryDirectory() as folder:
            tools = ToolController(Path(folder) / "tools.json")
            p = self.panel(page="blob")
            p.tools_open = True
            p.tool_view = "calculator"
            self.draw(p, tools=tools)
            self.assertIn("calc:mc", p.rects)
            self.assertIn("calc:Deg", p.rects)
            self.assertIn("calc:7", p.rects)
            self.assertIn("calc:=", p.rects)
            self.assertEqual(p.w, p.TOOL_W * p.S)
            self.assertEqual(p.height(self.snap), p.TOOL_H * p.S)
            self.assert_bounds(p)

    def test_magnifier_handle_label_clears_the_reading_aperture_at_all_scales(self):
        for scale in (1, 1.25, 1.5, 2):
            p = self.panel(scale=scale)
            p.tools_open, p.tool_view = True, "magnifier"
            self.draw(p)
            self.assert_bounds(p)
            self.assertEqual(p.rects, {})  # wheel and right-click, no reading obstructions
            for text, bounds in p.labels:
                self.assertFalse(intersects(bounds, p.magnifier_aperture()), text)

    def test_sound_bubble_has_status_and_hover_restore_satellite(self):
        p = self.panel(page="sound")
        p.sound_view = "bubble"
        self.draw(p)
        self.assertEqual(p.w, p.BUBBLE_W * p.S)
        self.assertEqual(p.height(self.snap), p.BUBBLE_H * p.S)
        self.assertNotIn("sview:card", p.rects)
        self.assertIn("toggle:sound", p.rects)
        self.assertIn("+8", [text for text, _ in p.labels])
        p.tool_reveal = 1.0
        self.draw(p)
        self.assertIn("sview:card", p.rects)
        self.assertEqual(p.hit(p.bubble_x(82.5), 22.5 * p.S), "sview:card")
        self.assert_bounds(p)

    def test_sound_output_menu_lists_physical_destinations(self):
        p = self.panel(page="sound")
        p.sound_menu = True
        self.draw(p)
        self.assertIn("Output", [text for text, _ in p.labels])
        self.assertIn("output:0", p.rects)
        self.assertIn("output:1", p.rects)
        for key in ("output:0", "output:1"):
            box = p.rects[key]
            self.assertEqual(p.hit((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), key)
        self.assert_bounds(p)

    def test_missing_audio_driver_has_setup_action_in_both_sizes(self):
        self.sound.cable = False
        for compact in (False, True):
            p = self.panel(compact=compact, page="sound")
            self.draw(p)
            self.assertIn("setupaudio", p.rects)
            self.assert_bounds(p)

    def test_screenshot_setting_is_explicit(self):
        p = self.panel(page="settings")
        seen = []
        for row in range(20):
            p.scroll["settings"] = row
            self.draw(p)
            seen.extend(t for t, _ in p.labels)
        self.assertTrue(any("screenshots" in text for text in seen))
        self.assertTrue(any("recordings" in text for text in seen))

    def test_empty_snapshot_keeps_panel_renderable_during_discovery(self):
        p = self.panel()
        p.draw(blob.empty_snapshot(), self.sound, .35, False, media=self.media)
        self.assert_bounds(p)

    def test_sensor_list_bounded_and_scrollable(self):
        p = self.panel()
        p.details_open = True
        self.draw(p)
        self.assertLess(p.height(self.snap), p.max_height())
        first = [t for t, _ in p.labels]
        p.scroll["details"] = 1000
        self.draw(p)
        self.assert_bounds(p)
        self.assertIn("Battery", [t for t, _ in p.labels])
        self.assertNotEqual(first, [t for t, _ in p.labels])

    def test_hardware_tab_uses_contextual_card_and_mode_bubble(self):
        self.assertEqual(dict(blob.PAGES)["blob"], "System")
        p = self.panel(page="blob")
        self.draw(p)
        self.assertIn("hview:bubble", p.rects)
        self.assertIn("details", p.rects)
        bubble_parts = [c for c in p.controls if c[0] == "hover" and c[1] == "hview:bubble"]
        self.assertEqual(len(bubble_parts), 1)
        box = p.rects["hview:bubble"]
        self.assertAlmostEqual(box[2] - box[0], box[3] - box[1])
        self.assertEqual({k.split(":", 1)[1] for k in p.rects if k.startswith("hmode:")},
                         {"auto", "quiet", "balanced", "performance", "custom"})
        for mode, label in blob.HARDWARE_MODES:
            box = p.rects["hmode:" + mode]
            self.assertLessEqual(p.text_font(label, 11).getlength(label), box[2] - box[0] - 4 * p.S)

        p.tabs_open, p.tabs_t = True, 1
        self.draw(p)
        self.assertNotIn("details", p.rects)

        p.hardware_view = "bubble"
        self.draw(p)
        self.assertEqual(p.w, p.BUBBLE_W * p.S)
        self.assertEqual(p.height(self.snap), p.BUBBLE_H * p.S)
        self.assertIn("hcycle", p.rects)
        self.assertNotIn("hview:card", p.rects)
        self.assertNotIn("tabs", p.rects)
        self.assert_bounds(p)

    def test_left_edge_control_rail_clears_page_headers(self):
        cases = (("blob", "hview:bubble", "CPU"),
                 ("sound", "sview:bubble", "Output"),
                 ("settings", "settingsview:bubble", "APPEARANCE"))
        for page, control, heading in cases:
            for compact in (False, True):
                with self.subTest(page=page, compact=compact):
                    p = self.panel(compact=compact, page=page)
                    p.anchor_side = "left"
                    self.draw(p)
                    box = p.rects[control]
                    wanted = "Boost" if page == "sound" and compact else heading
                    label_box = next(bounds for text, bounds in p.labels if text == wanted)
                    self.assertFalse(intersects(box, label_box), (page, compact, box, label_box))
                    self.assert_bounds(p)

    def test_hardware_details_include_discovered_fans(self):
        self.snap["fans"] = [dict(name="Front intake", rpm=820), dict(name="AIO pump", rpm=2100)]
        rows = self.panel().detail_rows(self.snap)
        self.assertIn(("Front intake", "820 rpm", None), rows)
        self.assertIn(("AIO pump", "2,100 rpm", None), rows)

    def test_transport_does_not_intersect_seek_artwork_or_switcher(self):
        for compact in (False, True):
            for view in ("now", "art"):
                for tabs in (0, 1):
                    p = self.panel(compact=compact, page="music", view=view)
                    p.tabs_t = tabs
                    if view == "art":
                        p.hover_key = "arthover"
                    self.draw(p)
                    relevant = [(k, b) for k, b in p.rects.items()
                                if k.startswith(("media:", "am:", "musicutil:", "page:", "slider:seek")) or
                                k in ("tabs", "mview:art", "mview:now")]
                    for i, (key, box) in enumerate(relevant):
                        for other, rect in relevant[i + 1:]:
                            self.assertFalse(intersects(box, rect), (compact, view, tabs, key, other))

    def test_art_view_displays_seek_preview(self):
        p = self.panel(page="music", view="art")
        p.hover_key = "arthover"
        self.draw(p, seek=.75)
        self.assertEqual(next(c[-1] for c in p.controls if c[:2] == ("slider", "seek")), .75)

    def test_artwork_hover_reveals_contextual_controls(self):
        p = self.panel(page="music", view="art")
        self.draw(p)
        self.assertIn("arthover", p.rects)
        self.assertNotIn("media:toggle", p.rects)
        p.hover_key = "arthover"
        self.draw(p)
        self.assertIn("mview:now", p.rects)
        self.assertIn("media:toggle", p.rects)
        self.assertNotIn("tabs", p.rects)

    def test_artwork_hover_overlay_preserves_all_four_rounded_corners(self):
        p = self.panel(page="music", view="art")
        p.hover_key = "arthover"
        self.draw(p)
        pad = round(p.pad_u * p.S)
        side = round(p.w - 2 * pad)
        for point in ((pad, pad), (pad+side-1, pad),
                      (pad, pad+side-1), (pad+side-1, pad+side-1)):
            self.assertEqual(p.pic.getpixel(point)[3], 0, point)

    def test_list_status_clears_bottom_navigation(self):
        for compact in (False, True):
            for view in ("search", "queue"):
                p = self.panel(compact=compact, page="music", view=view)
                self.draw(p)
                for text, box in p.labels:
                    if text.startswith("A very long playback"):
                        self.assertLess(box[3], p.ink.height - 56 * p.S)

    def test_sound_conflict_does_not_overlap_quit_button(self):
        self.sound.fx_conflict = True
        p = self.panel(page="sound")
        self.draw(p)
        for text, box in p.labels:
            if text not in ("Quit FxSound", "Use Blob audio"):
                self.assertFalse(intersects(box, p.rects["fxquit"]), text)

    def test_fit_handles_tiny_width_and_emoji_font(self):
        p = self.panel()
        self.assertEqual(p.fit("long", 13, "Regular", 0), "")
        shown = p.fit("🎵 A long title with music", 13, "Regular", 100)
        self.assertLessEqual(p.text_font(shown, 13).getlength(shown), 100)


class MediaTests(unittest.TestCase):
    def player(self):
        m = NowPlaying.__new__(NowPlaying)
        m.position, m.duration, m.stamp, m.playing = 30, 300, 100, True
        m.cmds = queue.Queue()
        return m

    def test_pause_keeps_elapsed_position(self):
        m = self.player()
        with patch("media.time.time", return_value=110):
            m.toggle()
        self.assertEqual(m.position, 40)
        self.assertFalse(m.playing)

    def test_resume_does_not_add_paused_time(self):
        m = self.player()
        m.playing = False
        with patch("media.time.time", return_value=200):
            m.toggle()
        self.assertEqual(m.position, 30)
        self.assertTrue(m.playing)

    def test_lost_session_clears_timeline_and_art(self):
        m = self.player()
        m.art, m.art_version, m.can_seek = object(), 2, True
        with patch.object(m, "_pick", return_value=None):
            asyncio.run(m._poll(None))
        self.assertEqual((m.duration, m.position, m.art_version), (0, 0, 3))
        self.assertIsNone(m.art)
        self.assertFalse(m.can_seek)

    def test_toggle_dispatches_explicit_pause_for_playing_session(self):
        class Session:
            source_app_user_model_id = "AppleInc.AppleMusicWin_nzyj5cx40ttqa!App"

            def __init__(self):
                self.pause = Mock(return_value=True)
                self.play = Mock(return_value=True)
                self.toggle_play_pause = Mock(return_value=True)

            def get_playback_info(self):
                return NS(playback_status=4)

            async def try_pause_async(self):
                return self.pause()

            async def try_play_async(self):
                return self.play()

            async def try_toggle_play_pause_async(self):
                return self.toggle_play_pause()

        m = self.player()
        session = Session()
        with patch.object(m, "_pick", return_value=session):
            asyncio.run(m._command(NS(), "toggle"))
        session.pause.assert_called_once_with()
        session.play.assert_not_called()
        session.toggle_play_pause.assert_not_called()


class RendererTests(unittest.TestCase):
    def test_control_lenses_use_layout_scale(self):
        app = blob.App.__new__(blob.App)
        app.S, app.panel, app.springs = 1.5, blob.Panel(1.5), blob.Springs()
        app.slider_drag = app.hover = None
        lenses, _ = app.resolve([("slider", "boost", 30, 300, 90, .5)], 1, "")
        self.assertEqual(lenses[-1]["r"], 12 * app.panel.S)

    def test_fast_mask_matches_previous_coverage(self):
        for w, h, r in ((40, 40, 12), (90, 50, 20), (3, 30, 1)):
            np.testing.assert_array_equal(glass.shape_mask(w, h, r), glass.squircle_field(w, h, r)[0])

    def test_mask_does_not_compute_distance_fields(self):
        with patch.object(glass, "squircle_field", side_effect=AssertionError("expensive path")):
            self.assertEqual(glass.shape_mask(199, 199, 11).shape, (199, 199))

    def test_gdi_capture_reuses_growing_buffer(self):
        created = []
        def dib(w, h):
            d = NS(w=w, h=h, dc=1, arr=np.ones((h, w, 4), np.uint8), free=Mock())
            created.append(d)
            return d
        with patch.object(glass, "_gdi_dib", None), patch.object(glass, "Dib", side_effect=dib), \
             patch.object(glass.gdi32, "BitBlt", return_value=True):
            for h in (60, 40, 50, 80, 65):
                out = np.zeros((h, 40, 4), np.uint8)
                glass.gdi_grab(1, 0, 0, 40, h, out)
                self.assertTrue(out.all())
            self.assertEqual(len(created), 2)
            created[0].free.assert_called_once()

    def test_cross_monitor_capture_fills_entire_region(self):
        source = glass.ScreenSource.__new__(glass.ScreenSource)
        source.frozen, source.screen_dc = False, 1
        cam = Mock()
        source.outputs = [(0, 0, 100, 100, cam)]
        out = np.zeros((40, 40, 4), np.uint8)
        with patch.object(glass, "gdi_grab") as grab:
            self.assertTrue(source.grab(70, 20, 40, 40, out, False))
            grab.assert_called_once()
            cam.grab.assert_not_called()

    def test_capture_throttles_dragged_geometry_to_the_refresh_interval(self):
        renderer = glass.GlassRenderer.__new__(glass.GlassRenderer)
        renderer.sp, renderer.M = 8, 12
        renderer.cap = np.zeros((96, 160, 4), np.uint8)
        renderer.panel_y = lambda height: 0
        renderer._last_key = None
        renderer._last_capture = 0.0
        renderer.capture_min_interval = 1 / 60
        renderer._bg_dirty = False
        renderer.stats = {}
        renderer.source = NS(grab=Mock(return_value=True))
        with patch.object(glass.time, "perf_counter",
                          side_effect=[1.0, 1.0, 1.0, 1.005, 1.005,
                                       1.018, 1.018, 1.018]):
            self.assertTrue(renderer.capture(0, 0, 120, 80))
            self.assertFalse(renderer.capture(5, 0, 120, 80))
            self.assertTrue(renderer.capture(8, 0, 120, 80))
        self.assertEqual(renderer.source.grab.call_count, 2)


class HardwareTests(unittest.TestCase):
    def test_rest_sensor_tree_extracts_temperatures_and_named_fans(self):
        provider = blob.engine.HardwareMonitorWMI()
        tree = {"Children": [{"Text": "CPU", "Children": [
            {"Text": "CPU Package", "Type": "Temperature", "SensorId": "/cpu/0/temperature/0",
             "RawValue": "47.5"},
            {"Text": "CPU Fan", "Type": "Fan", "SensorId": "/lpc/0/fan/0",
             "Value": "1,280 RPM"},
        ]}]}
        rows = provider._rest_sensors(tree)
        self.assertEqual([row["Name"] for row in rows], ["CPU Package", "CPU Fan"])
        self.assertEqual([row["Value"] for row in rows], [47.5, 1280.0])

    def test_sensor_provider_uses_installed_elevated_task_with_cooldown(self):
        provider = blob.engine.HardwareMonitorWMI()
        with patch.object(blob.engine.time, "time", side_effect=[100, 110, 131]), \
             patch.object(blob.engine.subprocess, "run") as run:
            provider.start_installed_provider()
            provider.start_installed_provider()
            provider.start_installed_provider()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0], ["schtasks", "/Run", "/TN", "Blob Sensors"])

    def test_non_nvidia_adapter_is_primary_gpu(self):
        mon = blob.engine.Monitor.__new__(blob.engine.Monitor)
        tag = "0x00000000_0x00000001"
        mon.adapters = [(tag, "AMD Radeon RX 7800 XT")]
        mon.pdh = NS(values=lambda key: {
            f"pid_1_luid_{tag}_phys_0_eng_0_engtype_3D": 73.0
        })
        primary, secondary = mon.gpu_loads()
        self.assertEqual(primary, 73.0)
        self.assertIsNone(secondary)


class LifecycleTests(unittest.TestCase):
    @staticmethod
    def gaming_drag_app(view="horizontal", side="right", pos=None):
        class FakeGlass:
            sp, W, H = 28, 680, 900

            def __init__(self, panel_side):
                self.panel_side = panel_side

            def panel_x(self, width):
                return self.sp if self.panel_side == "left" else self.W-self.sp-round(width)

            def panel_y(self, height):
                return self.H-self.sp-round(height)

            def set_panel_side(self, panel_side):
                self.panel_side = panel_side

        app = blob.App.__new__(blob.App)
        app.S, app.ss = 1.0, blob.Panel.SS
        app.panel = blob.Panel(1)
        app.panel.page, app.panel.gaming_view = "gaming", view
        app.panel.gaming_restore_view = view
        app.panel.anchor_side = side
        app.panel.update_width()
        app.panel_side = side
        app.glass = FakeGlass(side)
        app.snap = fixtures()[0]
        app.springs = blob.Springs()
        app.springs.get("width", app.panel.w, k=240, zeta=.90)
        app.springs.get("height", app.panel.height(app.snap), k=320, zeta=.86)
        app.springs.get("hardware_morph", 0.0, k=220, zeta=.86)
        app.pos = list(pos or (72, -758))
        app.visible, app.full, app.drag = True, False, None
        app.drag_work = None
        app.gaming_dock_edge = None
        app.frame_dirty, app.pinned = False, True
        app.draw_content, app.old_page_springs = Mock(), Mock()
        return app

    def test_panel_work_area_clamp_keeps_the_full_glass_and_action_rail_visible(self):
        app = self.gaming_drag_app(pos=(1600, 1000))
        work = NS(left=0, top=0, right=1600, bottom=900)
        with patch.object(blob, "work_area_at", return_value=(work, None)):
            self.assertTrue(app.keep_panel_in_work_area())
        x, y, width, height = app._panel_screen_rect()
        guard = app.glass.sp + 1
        self.assertGreaterEqual(x-guard, work.left)
        self.assertGreaterEqual(y-guard, work.top)
        self.assertLessEqual(x+width+guard, work.right)
        self.assertLessEqual(y+height+guard, work.bottom)

    def test_tabs_texture_updates_are_coalesced_while_the_spring_stays_live(self):
        app = blob.App.__new__(blob.App)
        app.panel = NS(tabs_t=0.0)
        app._tabs_texture_at = 0.0
        tabs = NS(x=.18, moving=True)
        self.assertTrue(app._sync_tabs_texture(1.0, tabs))
        tabs.x = .40
        self.assertFalse(app._sync_tabs_texture(1.01, tabs))
        self.assertTrue(app._sync_tabs_texture(1.0 + blob.TAB_TEXTURE_SECONDS + .001, tabs))
        tabs.x, tabs.moving = 1.0, False
        self.assertTrue(app._sync_tabs_texture(1.0 + 2 * blob.TAB_TEXTURE_SECONDS + .002, tabs))
        self.assertEqual(app.panel.tabs_t, 1.0)

    def test_gaming_edge_drop_maps_top_to_horizontal_and_sides_to_vertical(self):
        work = NS(left=0, top=0, right=1600, bottom=900)
        top = self.gaming_drag_app(view="horizontal", pos=(72, -758))
        with patch.object(blob, "work_area_at", return_value=(work, None)), \
                patch.object(blob.engine, "load_config", return_value={}), \
                patch.object(blob.engine, "save_config"):
            self.assertTrue(top.settle_gaming_edge_dock())
        self.assertEqual((top.gaming_dock_edge, top.panel.gaming_view), ("top", "horizontal"))
        self.assertAlmostEqual(top._panel_screen_rect()[1], top.glass.sp + 1)

        side = self.gaming_drag_app(view="horizontal", pos=(1, -658))
        with patch.object(blob, "work_area_at", return_value=(work, None)), \
                patch.object(blob.engine, "load_config", return_value={}), \
                patch.object(blob.engine, "save_config"):
            self.assertTrue(side.settle_gaming_edge_dock())
        self.assertEqual((side.gaming_dock_edge, side.panel.gaming_view, side.panel_side),
                         ("left", "vertical", "left"))
        side.springs["width"].x = side.panel.w
        side.springs["height"].x = side.panel.height(side.snap)
        with patch.object(blob, "work_area_at", return_value=(work, None)):
            side.maintain_gaming_edge_dock()
        self.assertAlmostEqual(side._panel_screen_rect()[0], side.glass.sp + 1)

    def test_hardware_view_change_springs_geometry_and_shape(self):
        app = blob.App.__new__(blob.App)
        app.panel = blob.Panel(1)
        app.snap = fixtures()
        app.springs = blob.Springs()
        app.glass = NS(panel_y=lambda height: 777 - height)
        app.ss = blob.Panel.SS
        old_width, old_height = app.panel.w, app.panel.height(app.snap)
        app.springs.get("width", old_width, k=185, zeta=.86)
        app.springs.get("height", old_height, k=260, zeta=.82)
        app.springs.get("hardware_morph", 0, k=155, zeta=.82)
        with patch.object(blob.engine, "load_config", return_value={}), \
             patch.object(blob.engine, "save_config"):
            app.set_hardware_view("bubble")
        self.assertEqual(app.springs["width"].x, old_width)
        self.assertEqual(app.springs["height"].x, old_height)
        self.assertEqual(app.springs["width"].target, blob.Panel.BUBBLE_W * app.panel.S)
        self.assertEqual(app.springs["height"].target, blob.Panel.BUBBLE_H * app.panel.S)
        self.assertEqual(app.springs["hardware_morph"].x, 0)
        self.assertEqual(app.springs["hardware_morph"].target, 1)
        self.assertEqual(app.hardware_top, 777 - round(old_height / app.ss))
        self.assertEqual(app._panel_y(49), app.hardware_top)
        app.panel.hardware_view = "card"
        app.springs["hardware_morph"].x = 0
        self.assertEqual(app._panel_y(49), 777 - 49)

    def test_hotkey_parser_requires_a_modifier_and_formats_binding(self):
        self.assertEqual(blob.parse_hotkey({"mods": blob.MOD_CONTROL | blob.MOD_SHIFT,
                                            "vk": ord("K")}),
                         (blob.MOD_CONTROL | blob.MOD_SHIFT, ord("K")))
        self.assertEqual(blob.hotkey_label(blob.MOD_CONTROL | blob.MOD_SHIFT, ord("K")),
                         "Ctrl + Shift + K")
        self.assertEqual(blob.parse_hotkey({"mods": 0, "vk": ord("Q")}), blob.DEFAULT_HOTKEY)

    def test_music_view_change_preserves_card_top_and_morphs(self):
        app = blob.App.__new__(blob.App)
        app.panel = blob.Panel(1)
        app.panel.page = "music"
        app.snap = fixtures()[0]
        app.springs = blob.Springs()
        app.glass = NS(panel_y=lambda height: 900 - height)
        app.ss = blob.Panel.SS
        old_width, old_height = app.panel.w, app.panel.height(app.snap)
        app.springs.get("width", old_width, k=185, zeta=.86)
        app.springs.get("height", old_height, k=260, zeta=.82)
        app.springs.get("music_morph", 0, k=155, zeta=.82)
        with patch.object(blob.engine, "load_config", return_value={}), \
             patch.object(blob.engine, "save_config"):
            app.set_music_view("bubble")
        self.assertEqual(app.springs["width"].target, blob.Panel.BUBBLE_W * app.panel.S)
        self.assertEqual(app.springs["height"].target, blob.Panel.BUBBLE_H * app.panel.S)
        self.assertEqual(app.springs["music_morph"].target, 1)
        self.assertEqual(app.music_top, 900 - round(old_height / app.ss))
        self.assertEqual(app._panel_y(49), app.music_top)

    def test_sound_view_change_preserves_card_top_and_morphs(self):
        app = blob.App.__new__(blob.App)
        app.panel = blob.Panel(1)
        app.panel.page = "sound"
        app.snap = fixtures()[0]
        app.springs = blob.Springs()
        app.glass = NS(panel_y=lambda height: 900 - height)
        app.ss = blob.Panel.SS
        old_width, old_height = app.panel.w, app.panel.height(app.snap)
        app.springs.get("width", old_width, k=185, zeta=.86)
        app.springs.get("height", old_height, k=260, zeta=.82)
        app.springs.get("sound_morph", 0, k=155, zeta=.82)
        with patch.object(blob.engine, "load_config", return_value={}), \
                patch.object(blob.engine, "save_config"):
            app.set_sound_view("bubble")
        self.assertEqual(app.panel.sound_view, "bubble")
        self.assertEqual(app.springs["width"].target, blob.Panel.BUBBLE_W * app.panel.S)
        self.assertEqual(app.springs["height"].target, blob.Panel.BUBBLE_H * app.panel.S)
        self.assertEqual(app.springs["sound_morph"].target, 1)
        self.assertEqual(app.sound_top, 900 - round(old_height / app.ss))
        self.assertEqual(app._panel_y(49), app.sound_top)

    def test_gaming_view_change_preserves_orientation_for_bubble_restore(self):
        app = blob.App.__new__(blob.App)
        app.panel = blob.Panel(1)
        app.panel.page = "gaming"
        app.panel.gaming_view = "vertical"
        app.snap = fixtures()[0]
        app.springs = blob.Springs()
        app.glass = NS(panel_y=lambda height: 900 - height)
        app.ss = blob.Panel.SS
        old_width, old_height = app.panel.w, app.panel.height(app.snap)
        app.springs.get("width", old_width, k=185, zeta=.86)
        app.springs.get("height", old_height, k=260, zeta=.82)
        app.springs.get("hardware_morph", 0, k=155, zeta=.82)
        with patch.object(blob.engine, "load_config", return_value={}), \
                patch.object(blob.engine, "save_config"):
            self.assertTrue(app.set_gaming_view("bubble"))
        self.assertEqual(app.panel.gaming_restore_view, "vertical")
        self.assertEqual(app.springs["width"].target, blob.Panel.BUBBLE_W * app.panel.S)
        self.assertEqual(app.springs["height"].target, blob.Panel.BUBBLE_H * app.panel.S)
        self.assertEqual(app.springs["hardware_morph"].target, 1)
        self.assertEqual(app.gaming_top, 900 - round(old_height / app.ss))
        with patch.object(blob.engine, "load_config", return_value={}), \
                patch.object(blob.engine, "save_config"):
            self.assertTrue(app.set_gaming_view("vertical"))
        self.assertEqual(app.panel.gaming_view, "vertical")
        self.assertEqual(app.panel.gaming_restore_view, "vertical")
        self.assertEqual(app.springs["width"].target,
                         blob.Panel.GAMING_VERTICAL_WIDE * app.panel.S)
        self.assertEqual(app.springs["height"].target,
                         blob.Panel.GAMING_VERTICAL_H * app.panel.S)
        self.assertEqual(app.springs["hardware_morph"].target, 0)

    def test_music_bubble_tap_double_tap_and_hold(self):
        app = blob.App.__new__(blob.App)
        app.hwnd = 101
        app.panel = NS(page="music", music_view="bubble")
        app.visible = True
        app.gaming_unlocked = app.gaming_modifier_drag = False
        app.media = Mock()
        app.music_press_active = app.music_hold_fired = app.music_click_pending = False
        with patch.object(blob, "user32") as user32:
            user32.GetCapture.return_value = app.hwnd
            app.start_music_bubble_press()
            app.finish_music_bubble_press()
            self.assertTrue(app.music_click_pending)
            app.start_music_bubble_press()
            app.finish_music_bubble_press()
            app.media.toggle.assert_called_once_with()
            app.media.next.assert_called_once_with()

            app.start_music_bubble_press()
            app.wndproc(app.hwnd, blob.WM_TIMER, 5, 0)
            app.media.previous.assert_called_once_with()
            app.finish_music_bubble_press()
            self.assertFalse(app.music_click_pending)

    def test_music_bubble_single_tap_is_immediate_and_timer_only_clears_double_tap_window(self):
        app = blob.App.__new__(blob.App)
        app.hwnd = 101
        app.panel = NS(page="music", music_view="bubble")
        app.visible = True
        app.gaming_modifier_drag = False
        app.media = Mock()
        app.music_press_active = app.music_hold_fired = app.music_click_pending = False
        with patch.object(blob.user32, "GetCapture", return_value=app.hwnd), \
             patch.object(blob.user32, "SetTimer"), patch.object(blob.user32, "KillTimer"), \
             patch.object(blob.user32, "ReleaseCapture"):
            app.start_music_bubble_press()
            app.finish_music_bubble_press()
            app.media.toggle.assert_called_once_with()
            self.assertTrue(app.music_click_pending)
            app.wndproc(app.hwnd, blob.WM_TIMER, 4, 0)
        self.assertFalse(app.music_click_pending)

    def test_music_reactivity_toggle_is_shared_and_persisted(self):
        app = blob.App.__new__(blob.App)
        app.panel = NS(options={"musicreactive": True})
        app.draw_content, app.frame = Mock(), Mock()
        cfg = {"options": {"musicreactive": True}}
        with patch.object(blob.engine, "load_config", return_value=cfg), \
             patch.object(blob.engine, "save_config") as save:
            app.click("toggle:musicreactive", 0)
        self.assertFalse(app.panel.options["musicreactive"])
        self.assertFalse(cfg["options"]["musicreactive"])
        save.assert_called_once_with(cfg)

    def test_sound_toggle_routes_to_audio_controller(self):
        app = blob.App.__new__(blob.App)
        app.panel = NS(options={})
        app.sound = NS(enabled=False, set_enabled=Mock())
        app.draw_content = app.frame = Mock()
        app.click("toggle:sound", 0)
        app.sound.set_enabled.assert_called_once_with(True)

    def test_sound_output_menu_selects_named_destination(self):
        app = blob.App.__new__(blob.App)
        app.panel = blob.Panel(1)
        app.panel.sound_menu = False
        app.sound = NS(output_options=Mock(return_value=["Realtek", "Odyssey"]),
                       select_output=Mock(), refresh_devices=Mock())
        app.draw_content = Mock()
        app.frame = Mock()
        app.click("device", 0)
        self.assertTrue(app.panel.sound_menu)
        app.sound.refresh_devices.assert_called_once_with()
        app.click("output:1", 0)
        app.sound.select_output.assert_called_once_with("Odyssey")
        self.assertFalse(app.panel.sound_menu)

    def test_hardware_bubble_cycles_only_quick_modes(self):
        app = blob.App.__new__(blob.App)
        app.panel = blob.Panel(1)
        with patch.object(blob.engine, "load_config", return_value={}), \
             patch.object(blob.engine, "save_config"):
            seen = []
            for _ in range(5):
                app.cycle_hardware_mode()
                seen.append(app.panel.hardware_mode)
        self.assertEqual(seen, ["quiet", "balanced", "performance", "auto", "quiet"])
        self.assertNotIn("custom", seen)
        self.assertEqual(dict(blob.HARDWARE_MODES)["performance"], "Turbo")

    def test_audio_launches_have_separate_shared_buffers(self):
        with patch.object(sound.threading, "Thread"), patch.object(sound.core, "load_config", return_value={}):
            first, second = sound.Sound(), sound.Sound()
        try:
            self.assertNotEqual(first.shm.name, second.shm.name)
            first.p[sound.QUIT] = 1
            self.assertEqual(second.p[sound.QUIT], 0)
        finally:
            for s in (first, second):
                s.p = None
                s.shm.close()
                s.shm.unlink()

    def test_boost_at_launch_does_not_depend_on_previous_toggle(self):
        cfg = {"options": {"soundstart": True}, "sound": {"enabled": False}}
        with patch.object(sound.threading, "Thread"), patch.object(sound.core, "load_config", return_value=cfg), \
             patch.object(sound.Sound, "set_enabled") as enable:
            s = sound.Sound()
        try:
            enable.assert_called_once_with(True)
        finally:
            s.p = None
            s.shm.close()
            s.shm.unlink()

    def test_screenshot_cleanup_preserves_recording_preference(self):
        for enabled in (False, True):
            app = blob.App.__new__(blob.App)
            app.capture_until, app.captureable, app.hwnd = 1, enabled, 1
            app.panel = NS(magnifier_active=False)
            app.glass = NS(source=NS(frozen=True))
            with patch.object(blob.time, "time", return_value=30), \
                 patch.object(blob.user32, "SetWindowDisplayAffinity") as affinity:
                app._maybe_end_capture()
            affinity.assert_called_once_with(1, 0 if enabled else 0x11)
            self.assertEqual(app.glass.source.frozen, enabled)


if __name__ == "__main__":
    unittest.main()
