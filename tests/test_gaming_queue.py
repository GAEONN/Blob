import io
import queue
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import test_regressions as reg
import applemusic
import gaming


class GamingLayoutTests(unittest.TestCase):
    setUp = reg.LayoutTests.setUp
    panel = reg.LayoutTests.panel
    draw = reg.LayoutTests.draw
    assert_bounds = reg.LayoutTests.assert_bounds
    def test_strip_metrics_and_navigation_at_all_scales(self):
        for scale in (1, 1.25, 1.5, 2):
            for compact in (False, True):
                for expanded in (False, True):
                    p = self.panel(scale, compact, "gaming")
                    p.game = dict(fps=144, frame_ms=6.94, ram_percent=62, ram_gb=19.8)
                    p.tabs_open, p.tabs_t = expanded, float(expanded)
                    self.draw(p)
                    self.assert_bounds(p)
                    labels = [t for t, _ in p.labels]
                    for metric in ("FPS", "CPU", "GPU", "RAM", "FANS", "GPU POWER", "6.9 ms"):
                        self.assertIn(metric, labels)
                    self.assertEqual(p.height(self.snap) / p.S, 142 if expanded else 86)
                    self.assertEqual(any(k.startswith("page:") for k in p.rects), expanded)
                    self.assertFalse(any(k.startswith("size:") for k in p.rects))

    def test_missing_fps_is_not_faked(self):
        p = self.panel(page="gaming")
        p.game = {"status": "FPS needs Windows Performance Log Users permission."}
        self.draw(p)
        self.assertIn("Setup needed", [t for t, _ in p.labels])
        self.assertNotIn("0", [t for t, _ in p.labels])
        p.game["status"] = "FPS unavailable. Capture stopped; retrying shortly."
        self.draw(p)
        self.assertIn("Retrying…", [t for t, _ in p.labels])


class FrameStatsTests(unittest.TestCase):
    def row(self, pid=10, ms=10, chain="a"):
        return dict(ProcessID=str(pid), msBetweenPresents=str(ms), SwapChainAddress=chain, Application="game.exe")

    def test_actual_presentmon_header_is_recognized(self):
        with patch.object(gaming.threading, "Thread"):
            m = gaming.GamingMonitor()
        proc = NS(stdout=io.StringIO("Application,ProcessID,SwapChainAddress,msBetweenPresents\ngame.exe,10,a,10\n"))
        m.proc = proc
        m._read(proc)
        self.assertEqual(m.stats.snapshot(10, time.monotonic())["fps"], 100)

    def test_weighted_frame_rate_and_stale_data(self):
        s = gaming.FrameStats()
        s.add(self.row(ms=10), 1)
        s.add(self.row(ms=30), 1.1)
        self.assertEqual(s.snapshot(10, 1.2)["fps"], 50)
        self.assertEqual(s.snapshot(10, 1.2)["frame_ms"], 20)
        self.assertIsNone(s.snapshot(10, 5))

    def test_processes_and_swapchains_are_not_mixed(self):
        s = gaming.FrameStats()
        s.add(self.row(), 1)
        s.add(self.row(), 1.1)
        s.add(self.row(pid=99, ms=50), 1)
        s.add(self.row(chain="b", ms=50), 1)
        self.assertEqual(s.snapshot(10, 1.2)["fps"], 100)
        self.assertEqual(s.snapshot(99, 1.2)["fps"], 20)

    def test_invalid_and_empty_frames(self):
        s = gaming.FrameStats()
        for ms in (0, -1, "nan", "NA", "inf"):
            s.add(self.row(ms=ms), 1)
        s.add({}, 1)
        self.assertIsNone(s.snapshot(10, 1))

    def test_helper_failure_after_samples_is_not_reported_as_no_game(self):
        m = gaming.GamingMonitor.__new__(gaming.GamingMonitor)
        m.status, m._stop = "Focus a game to see its FPS.", Mock()
        m._on_exit()
        self.assertIn("Capture stopped", m.status)
        m.status = "FPS needs Windows Performance Log Users permission."
        m._on_exit()
        self.assertIn("permission", m.status)


class QueueTests(unittest.TestCase):
    def player(self):
        with patch.object(applemusic.threading, "Thread"), patch.object(applemusic, "ThreadPoolExecutor"):
            a = applemusic.AppleMusic()
        a.jobs = queue.Queue()
        return a

    def test_refresh_is_single_flight_and_preserves_cached_rows(self):
        a = self.player()
        a.queue = [dict(title="Cached song", artist="Artist")]
        for _ in range(30):
            a.refresh_queue()
        self.assertEqual(a.jobs.qsize(), 1)
        self.assertTrue(a.queue_loading)
        self.assertEqual(a.queue[0]["title"], "Cached song")

    def test_recent_empty_queue_is_not_reloaded_each_frame(self):
        a = self.player()
        a._queue_stamp = time.monotonic()
        a.refresh_queue()
        self.assertTrue(a.jobs.empty())
        a.refresh_queue(force=True)
        self.assertEqual(a.jobs.qsize(), 1)

    def test_artwork_is_submitted_without_waiting_for_network(self):
        a = self.player()
        a.queue = [dict(title="Song", artist="Artist", art=None)] * 2
        a._queue_art()
        a._art_pool.submit.assert_called_once()
        a._queue_art()
        a._art_pool.submit.assert_called_once()

    def test_cached_art_is_immediate(self):
        a = self.player()
        art = object()
        a._art_cache[("Song", "Artist")] = art
        a.queue = [dict(title="Song", artist="Artist", art=None)]
        a._queue_art()
        self.assertIs(a.queue[0]["art"], art)
        a._art_pool.submit.assert_not_called()

    def test_queue_click_captures_track_identity_not_stale_index(self):
        a = self.player()
        a.queue = [dict(title="Song", artist="Artist")]
        a.play_queue(0)
        a.queue = [dict(title="Different track", artist="Different artist")]
        self.assertEqual(a.jobs.get_nowait(), ("play_queue", ("Song", "Artist")))

    def test_queue_toggle_has_no_library_default_sleep(self):
        a = self.player()
        toggle = NS(ToggleState=0, Toggle=Mock())
        btn = NS(GetTogglePattern=lambda: toggle)
        with patch.object(applemusic.time, "sleep"):
            self.assertTrue(a._queue_panel(None, True, btn))
        toggle.Toggle.assert_called_once_with(waitTime=0)

    def test_playback_error_wins_over_refresh_status(self):
        snap, sound, media, am = reg.fixtures()
        am.queue_action_status = "Couldn't play. Try again."
        am.queue_status = "Updating Playing Next…"
        p = reg.RecordingPanel(1)
        p.page, p.music_view, p.am = "music", "queue", am
        p.draw(snap, sound, .35, False, media=media)
        self.assertIn(am.queue_action_status, [t for t, _ in p.labels])
        self.assertNotIn(am.queue_status, [t for t, _ in p.labels])


if __name__ == "__main__":
    unittest.main()
