import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import admin_gaming


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def wait_for(cond):
    end = time.time() + 2
    while not cond() and time.time() < end:
        time.sleep(.01)


class GamingToggleTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.flag = self.dir / "gaming.on"
        self.clock = Clock()
        self.calls = []

    def make(self, code=0):
        def runner():
            if code is None:
                self.calls.append(1)
                raise OSError("no schtasks")
            self.flag.touch()  # the real task writes the flag right away
            self.calls.append(1)
            return NS(returncode=code)
        return admin_gaming.GamingModeToggle(self.flag, self.dir / "x.log", runner, self.clock)

    def test_toggle_is_locked_until_the_switch_settles(self):
        gm = self.make()
        self.assertFalse(gm.on)
        self.assertTrue(gm.toggle())
        self.assertTrue(gm.busy and gm.shown_on)
        self.assertEqual(gm.status_text(), "Switching…")
        self.assertFalse(gm.toggle())  # double click ignored
        wait_for(lambda: self.calls)
        self.clock.t += 10.5
        gm.poll()
        self.assertFalse(gm.busy)
        self.assertTrue(gm.on)
        self.assertEqual(gm.status_text(), "On")
        self.assertEqual(len(self.calls), 1)

    def test_missing_task_shows_setup_message(self):
        for code in (1, None):
            gm = self.make(code)
            gm.toggle()
            wait_for(lambda: gm.error)
            self.assertEqual(gm.error, admin_gaming.NOT_INSTALLED)
            self.assertFalse(gm.busy)

    def test_poll_reports_outside_changes(self):
        gm = self.make()
        gm.poll()
        self.assertFalse(gm.poll())
        self.flag.touch()
        self.clock.t += 1.1
        self.assertTrue(gm.poll())
        self.assertTrue(gm.on)


if __name__ == "__main__":
    unittest.main()
