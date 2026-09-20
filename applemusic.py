"""Direct Apple Music control for Windows.

Apple has no public API for the Windows app, so this drives the real Apple Music app:
  * search    — Apple's public catalog search (itunes.apple.com/search, no login)
  * play      — opens the song in Apple Music (musics:// link) and presses that song's own
                "Play “…”" entry through Windows UI Automation
  * playlists — read from the app's sidebar; playing one selects it and presses Play
  * queue     — "Playing Next" read out of the app's queue panel

Speed: the app only builds its page while it's actually on screen, so during an action its
window is made fully transparent and click-through ("ghosted") instead of minimised — Windows
treats it as visible, you never see it. When the pointer rests on a search result, that song's
page is preloaded, so a click only has to press Play."""
import contextlib
import ctypes
import io
import json
import os
import queue
import re
import threading
import time
import urllib.parse
import urllib.request

from PIL import Image

import engine as core

AUMID = "AppleInc.AppleMusicWin_nzyj5cx40ttqa!App"
u32 = ctypes.windll.user32
u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
u32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
u32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x80000, 0x20


def store_country():
    """Two-letter store region from Windows (e.g. 'mx'); Apple's catalog differs per country."""
    try:
        buf = ctypes.create_unicode_buffer(8)
        if ctypes.windll.kernel32.GetUserDefaultGeoName(buf, 8):
            return buf.value.lower()
    except (AttributeError, OSError):
        pass
    return "us"


class AppleMusic:
    def __init__(self, refocus=None):
        self.results = []          # [{title, artist, album, url, art, kind}]
        self.playlists = []        # [name]
        self.queue = []            # [{title, artist}] — Apple Music's Playing Next
        self.status = ""           # short message for the UI ("Searching…")
        self.version = 0           # bumps whenever results / playlists / status change
        self.country = store_country()
        self.refocus = refocus     # called after Apple Music grabbed focus, to hand it back
        self.page_url = None       # the song page currently loaded in the app
        self.last_link = 0.0
        self.jobs = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()
        self.jobs.put(("playlists", None))

    # ── UI-facing ──
    def search(self, term):
        term = term.strip()
        if term:
            self._set_status("Searching…")
            self.jobs.put(("search", term))

    def prefetch(self, i):
        """Pointer is resting on result i: load its page in the background."""
        if 0 <= i < len(self.results) and self.results[i].get("kind") == "song" \
                and self.results[i]["url"] != self.page_url and self.jobs.empty():
            self.jobs.put(("prefetch", self.results[i]))

    def play_result(self, i):
        if 0 <= i < len(self.results):
            r = self.results[i]
            self._set_status(f"Playing “{r['title']}”…")
            self.jobs.put(("play_playlist" if r.get("kind") == "playlist" else "play_song",
                           r["title"] if r.get("kind") == "playlist" else r))

    def play_playlist(self, name):
        self._set_status(f"Playing “{name}”…")
        self.jobs.put(("play_playlist", name))

    def refresh_playlists(self):
        self.jobs.put(("playlists", None))

    def refresh_queue(self):
        self.jobs.put(("queue", None))

    def play_queue(self, i):
        if 0 <= i < len(self.queue):
            self._set_status(f"Playing \u201c{self.queue[i]['title']}\u201d\u2026")
            self.jobs.put(("play_queue", i))

    def _set_status(self, s):
        self.status = s
        self.version += 1

    # ── worker ──
    def _run(self):
        import uiautomation as auto
        self.auto = auto
        with auto.UIAutomationInitializerInThread():
            auto.SetGlobalSearchTimeout(1)
            while True:
                job, arg = self.jobs.get()
                if job == "prefetch" and not self.jobs.empty():
                    continue  # something more important is waiting
                try:
                    if job == "search":
                        self._search(arg)
                    elif job == "prefetch":
                        self._prefetch(arg)
                    elif job == "play_song":
                        self._play_song(arg)
                        self._set_status("")
                    elif job == "play_playlist":
                        self._play_playlist(arg)
                        self._set_status("")
                    elif job == "playlists":
                        self._read_playlists()
                    elif job == "queue":
                        self._read_queue()
                    elif job == "play_queue":
                        self._play_queue(arg)
                        self._set_status("")
                except Exception as e:
                    core.log(f"applemusic {job}: {e!r}")
                    self.page_url = None
                    if job != "prefetch":
                        self._set_status("Apple Music didn't respond. Try again.")

    def _search(self, term):
        q = urllib.parse.urlencode({"term": term, "entity": "song", "limit": 12, "country": self.country})
        with urllib.request.urlopen("https://itunes.apple.com/search?" + q, timeout=8) as r:
            data = json.load(r)
        tracks = [t for t in data.get("results", []) if t.get("trackViewUrl")]

        def art(t):
            try:
                url = t.get("artworkUrl100", "").replace("100x100", "160x160")
                with urllib.request.urlopen(url, timeout=5) as r:
                    return Image.open(io.BytesIO(r.read())).convert("RGB")
            except Exception:
                return None
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as ex:
            arts = list(ex.map(art, tracks))
        songs = [{"title": t.get("trackName", ""), "artist": t.get("artistName", ""),
                  "album": t.get("collectionName", ""), "url": t["trackViewUrl"], "art": a, "kind": "song"}
                 for t, a in zip(tracks, arts)]
        low = term.lower()
        mine = [{"title": n, "artist": "Your playlist", "album": "", "url": None, "art": None,
                 "kind": "playlist"} for n in self.playlists if low in n.lower()][:4]
        self.results = mine + songs
        self._set_status("" if self.results else "Nothing found.")

    # ── the Apple Music window ──
    def _window(self, launch=True):
        w = self.auto.WindowControl(searchDepth=1, Name="Apple Music")
        if w.Exists(0.2):
            return w
        if not launch:
            return None
        os.startfile("shell:AppsFolder\\" + AUMID)
        for _ in range(40):
            time.sleep(0.5)
            if w.Exists(0.1):
                time.sleep(2.0)  # let it finish loading
                return w
        raise RuntimeError("Apple Music didn't open")

    @contextlib.contextmanager
    def _ghost(self, w):
        """Keep Apple Music on screen (so it renders) but invisible and click-through."""
        h = w.NativeWindowHandle
        old = u32.GetWindowLongPtrW(h, GWL_EXSTYLE) & ~WS_EX_LAYERED & ~WS_EX_TRANSPARENT
        try:
            u32.SetWindowLongPtrW(h, GWL_EXSTYLE, old | WS_EX_LAYERED | WS_EX_TRANSPARENT)
            u32.SetLayeredWindowAttributes(h, 0, 1, 2)
            if u32.IsIconic(h):
                u32.ShowWindow(h, 4)                                   # SW_SHOWNOACTIVATE
            u32.SetWindowPos(h, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # on top, no activation
            yield w
        finally:
            u32.ShowWindow(h, 7)                                       # SW_SHOWMINNOACTIVE
            u32.SetLayeredWindowAttributes(h, 0, 255, 2)
            u32.SetWindowLongPtrW(h, GWL_EXSTYLE, old)   # exactly what it was before we ghosted it
            if self.refocus:
                self.refocus()

    def _recover(self, w):
        """Apple Music sometimes shows 'An unknown error has occurred' — press its Try Again."""
        retry = w.ButtonControl(searchDepth=16, Name="Try Again")
        if retry.Exists(0):
            retry.GetInvokePattern().Invoke()
            time.sleep(1.0)
            return True
        return False

    def _open(self, w, song):
        """Navigate the app to the song's album page and wait for the song's row."""
        wait = 0.8 - (time.time() - self.last_link)  # rapid-fire links upset the app
        if wait > 0:
            time.sleep(wait)
        self.last_link = time.time()
        os.startfile(song["url"].replace("https://", "musics://", 1))
        if self.refocus:
            self.refocus()
        row = w.ListItemControl(searchDepth=12, RegexName=r"^Track \d+ " + re.escape(song["title"]) + " ")
        t_end = time.time() + 8
        next_check = time.time() + 1.0
        while time.time() < t_end:
            if row.Exists(0):
                self.page_url = song["url"]
                return row
            if time.time() > next_check:  # the error-page lookup is slow; only now and then
                next_check = time.time() + 1.0
                if self._recover(w):
                    os.startfile(song["url"].replace("https://", "musics://", 1))
            time.sleep(0.03)
        self.page_url = None
        return None

    def _prefetch(self, song):
        w = self._window(launch=False)
        if w is None:
            return
        with self._ghost(w):
            self._open(w, song)

    def _menu(self, w):
        menu = w.MenuControl(searchDepth=5)
        t_end = time.time() + 2
        while time.time() < t_end:
            if menu.Exists(0):
                return [c for c in menu.GetChildren() if c.ControlTypeName == "MenuItemControl"]
            time.sleep(0.02)
        return []

    def _play_song(self, song):
        w = self._window()
        with self._ghost(w):
            row = None
            if self.page_url == song["url"]:  # preloaded: the row is (re)built within moments
                row = w.ListItemControl(searchDepth=12,
                                        RegexName=r"^Track \d+ " + re.escape(song["title"]) + " ")
                t_end = time.time() + 1.5
                while not row.Exists(0) and time.time() < t_end:
                    time.sleep(0.03)
                if not row.Exists(0):
                    row = None
            if row is None:
                row = self._open(w, song)
            if row is not None:
                row.ButtonControl(searchDepth=4, Name="More").GetInvokePattern().Invoke()
                play = [m for m in self._menu(w)
                        if m.Name.startswith("Play ") and m.Name not in ("Play Next", "Play Last")]
                if play:
                    play[0].GetInvokePattern().Invoke()
                    return
            # fall back to playing the album the song is on
            btn = w.ButtonControl(searchDepth=12, AutomationId="PlayButtonElement")
            if btn.Exists(2):
                btn.GetInvokePattern().Invoke()
            else:
                raise RuntimeError("couldn't find the song")

    def _sidebar_playlists(self, w):
        items = []

        def walk(c, d=0):
            if d > 14:
                return
            for ch in c.GetChildren():
                if ch.ControlTypeName == "ListItemControl" and ch.AutomationId.startswith("DBID"):
                    items.append(ch)
                walk(ch, d + 1)
        walk(w)
        return items

    def _read_playlists(self):
        w = self._window(launch=False)
        if w is None:
            return
        seen, names = set(), []
        for it in self._sidebar_playlists(w):
            if it.Name and it.Name not in seen:
                seen.add(it.Name)
                names.append(it.Name)
        self.playlists = names
        self.version += 1

    def _queue_panel(self, w, want_open):
        """Toggle Apple Music's Playing Next panel."""
        btn = w.ButtonControl(searchDepth=10, AutomationId="PlayQueueToggleButton")
        if not btn.Exists(1):
            return False
        tog = btn.GetTogglePattern()
        is_open = bool(tog and tog.ToggleState == 1)
        if is_open != want_open:
            (tog.Toggle() if tog else btn.GetLegacyIAccessiblePattern().DoDefaultAction())
            time.sleep(0.9)
        return True

    def _read_queue(self):
        w = self._window(launch=False)
        if w is None:
            return
        with self._ghost(w):
            if not self._queue_panel(w, True):
                return
            items = []

            def walk(c, d=0):
                if d > 18:
                    return
                for ch in c.GetChildren():
                    if ch.ControlTypeName == "ListItemControl" and " \u2014 " in (ch.Name or ""):
                        texts = []

                        def grab(x, dd=0):
                            if dd > 4:
                                return
                            for k in x.GetChildren():
                                if k.ControlTypeName == "TextControl" and k.Name and len(k.Name) > 1 \
                                        and ord(k.Name[0]) < 0x10000:
                                    texts.append(k.Name)
                                grab(k, dd + 1)
                        grab(ch)
                        if texts:
                            items.append({"title": texts[0],
                                          "artist": " \u2014 ".join(texts[1:3]) if len(texts) > 1 else ""})
                    walk(ch, d + 1)
            walk(w)
            self.queue = items[:40]
            self._queue_panel(w, False)
        self.version += 1

    def _play_queue(self, index):
        w = self._window()
        with self._ghost(w):
            if not self._queue_panel(w, True):
                return
            items = []

            def walk(c, d=0):
                if d > 18:
                    return
                for ch in c.GetChildren():
                    if ch.ControlTypeName == "ListItemControl" and " \u2014 " in (ch.Name or ""):
                        items.append(ch)
                    walk(ch, d + 1)
            walk(w)
            if index < len(items):
                lp = items[index].GetLegacyIAccessiblePattern()
                if lp:
                    lp.DoDefaultAction()      # double click = play from here
                    time.sleep(0.4)
            self._queue_panel(w, False)
        self.jobs.put(("queue", None))

    def _play_playlist(self, name):
        w = self._window()
        with self._ghost(w):
            item = next((i for i in self._sidebar_playlists(w) if i.Name == name), None)
            if item is None:
                raise RuntimeError("playlist not found")
            item.GetSelectionItemPattern().Select()
            self.page_url = None
            btn = w.ButtonControl(searchDepth=12, AutomationId="PlayButton", Name="Play")
            t_end = time.time() + 4
            next_check = time.time() + 1.0
            while time.time() < t_end:
                if btn.Exists(0):
                    btn.GetInvokePattern().Invoke()
                    return
                if time.time() > next_check:
                    next_check = time.time() + 1.0
                    self._recover(w)
                time.sleep(0.03)
            raise RuntimeError("playlist page didn't load")
