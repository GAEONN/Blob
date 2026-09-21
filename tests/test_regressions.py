"""Offline regressions: no audio routing, sensor polling, or live desktop capture.

Run on Windows with: python -m unittest discover -s tests -v
"""
import asyncio
import importlib.machinery
import importlib.util
from pathlib import Path
import queue
import sys
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
                        self.assertLessEqual(box[3], p.ink.height - 78 * p.S)
            self.assertEqual(seen, expected)

    def test_music_uses_contextual_size_and_no_universal_size_control(self):
        sizes = set()
        for compact in (False, True):
            p = self.panel(compact=compact, page="music", view="now")
            p.tabs_t = 0
            self.draw(p)
            sizes.add((p.w, p.height(self.snap)))
            self.assertFalse(any(k.startswith("size:") for k in p.rects))
        self.assertEqual(len(sizes), 1)

        p = self.panel(page="music", view="art")
        self.draw(p)
        self.assertGreater(p.height(self.snap), next(iter(sizes))[1])
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
        for view in ("now", "art"):
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

    def test_music_bubble_has_gesture_body_and_restore_satellite(self):
        p = self.panel(page="music", view="bubble")
        self.draw(p)
        self.assertEqual(p.w, p.BUBBLE_W * p.S)
        self.assertEqual(p.height(self.snap), p.BUBBLE_H * p.S)
        self.assertIn("mbubble:gesture", p.rects)
        self.assertIn("mview:now", p.rects)
        self.assertNotIn("tabs", p.rects)
        # The fused lobes overlap visually; the satellite is registered last so
        # it wins hit testing in the shared neck while the body owns its centre.
        self.assertEqual(p.hit(79 * p.S, 22 * p.S), "mview:now")
        self.assertEqual(p.hit(43 * p.S, 62 * p.S), "mbubble:gesture")
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
        self.assertEqual(dict(blob.PAGES)["blob"], "Hardware")
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
        self.assertIn("hview:card", p.rects)
        self.assertNotIn("tabs", p.rects)
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
        for point in ((pad, round(p.CONTENT * p.S)),
                      (pad + side - 1, round(p.CONTENT * p.S)),
                      (pad, round(p.CONTENT * p.S) + side - 1),
                      (pad + side - 1, round(p.CONTENT * p.S) + side - 1)):
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
            if text != "Quit FxSound":
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
            app.media.next.assert_called_once_with()
            app.media.toggle.assert_not_called()

            app.start_music_bubble_press()
            app.wndproc(app.hwnd, blob.WM_TIMER, 5, 0)
            app.media.previous.assert_called_once_with()
            app.finish_music_bubble_press()
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
            app.glass = NS(source=NS(frozen=True))
            with patch.object(blob.time, "time", return_value=30), \
                 patch.object(blob.user32, "SetWindowDisplayAffinity") as affinity:
                app._maybe_end_capture()
            affinity.assert_called_once_with(1, 0 if enabled else 0x11)
            self.assertEqual(app.glass.source.frozen, enabled)


if __name__ == "__main__":
    unittest.main()
