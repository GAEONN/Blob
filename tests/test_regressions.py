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
                caps=dict(fan_control=True, nvidia=True), cpu_source="asus",
                sensors=[dict(name="Very long motherboard temperature sensor label " + str(i),
                              value=42, unit="°C", temp=True) for i in range(40)],
                power=dict(battery=81, plugged=True))
    sound = NS(enabled=True, boost=.5, boost_db=7.5, bass=.3, clarity=.3, surround=.25,
               preset="music", output="Speakers (Very Long USB Audio Device Name)",
               fx_conflict=False, error="", volume=.65, spectrum=np.zeros(28))
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
        p.hw_note = ("No ASUS fan interface: modes hidden.\n"
                     "Using the ACPI sensor. LibreHardwareMonitor or HWiNFO adds CPU and fan sensors.")
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
                                   ("music", "queue"), ("music", "search")):
                    with self.subTest(dpi=dpi, compact=compact, page=page, view=view):
                        p = self.panel(dpi, compact, page, view)
                        p.tabs_t = 1
                        self.draw(p)
                        self.assert_bounds(p)

    def test_settings_labels_clear_toggles_and_all_controls_reachable(self):
        expected = {"backdrop", "queueart", "artclick", "soundstart", "fanrestore", "startup", "capture"}
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

    def test_transport_does_not_intersect_seek_artwork_or_switcher(self):
        for compact in (False, True):
            for view in ("now", "art"):
                for tabs in (0, 1):
                    p = self.panel(compact=compact, page="music", view=view)
                    p.tabs_t = tabs
                    self.draw(p)
                    relevant = [(k, b) for k, b in p.rects.items()
                                if k.startswith(("media:", "am:", "page:", "slider:seek")) or
                                k in ("tabs", "mview:art", "mview:now")]
                    for i, (key, box) in enumerate(relevant):
                        for other, rect in relevant[i + 1:]:
                            self.assertFalse(intersects(box, rect), (compact, view, tabs, key, other))

    def test_art_view_displays_seek_preview(self):
        p = self.panel(page="music", view="art")
        self.draw(p, seek=.75)
        self.assertEqual(next(c[-1] for c in p.controls if c[:2] == ("slider", "seek")), .75)

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


class LifecycleTests(unittest.TestCase):
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
