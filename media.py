"""Now Playing: reads Windows' system media sessions (the same source as the Windows media
flyout) — title, artist, album art, position — and sends play/pause/next/previous/seek.
Apple Music is preferred when several apps are playing; anything else that plays works too."""
import asyncio
import datetime as dt
import io
import os
import queue
import threading
import time

from PIL import Image

import engine as core

APPLE_MUSIC_AUMID = "AppleInc.AppleMusicWin_nzyj5cx40ttqa!App"
FRIENDLY = {"AppleMusic": "Apple Music", "Chrome": "Chrome", "MSEdge": "Edge", "Spotify": "Spotify",
            "firefox": "Firefox", "iTunes": "iTunes"}


def friendly_source(aumid):
    for k, v in FRIENDLY.items():
        if k.lower() in (aumid or "").lower():
            return v
    return (aumid or "").split("!")[0].split(".")[-1] or "Media"


class NowPlaying:
    def __init__(self):
        self.title = self.artist = self.album = self.source = ""
        self.art = None           # PIL RGB image, or None
        self.art_version = 0
        self.playing = False
        self.active = False       # is there any session at all
        self.can_seek = False
        self.position = 0.0       # seconds at `stamp`
        self.duration = 0.0
        self.stamp = time.time()
        self.cmds = queue.Queue()
        self._key = None
        threading.Thread(target=self._run, daemon=True).start()

    # ── UI-facing ──
    def pos_now(self):
        p = self.position + (time.time() - self.stamp if self.playing else 0.0)
        return max(0.0, min(p, self.duration or p))

    def toggle(self):
        # Snapshot using the OLD playback state: otherwise pause loses elapsed time,
        # and resume incorrectly includes all the time spent paused.
        self.position, self.stamp = self.pos_now(), time.time()
        self.playing = not self.playing  # optimistic; the next poll confirms
        self.cmds.put("toggle")

    def next(self):
        self.cmds.put("next")

    def previous(self):
        self.cmds.put("previous")

    def seek(self, seconds):
        self.position, self.stamp = seconds, time.time()
        self.cmds.put(("seek", seconds))

    @staticmethod
    def open_apple_music():
        try:
            os.startfile("shell:AppsFolder\\" + APPLE_MUSIC_AUMID)
        except OSError:
            pass

    # ── background thread ──
    def _run(self):
        try:
            asyncio.run(self._main())
        except Exception as e:
            core.log(f"media: {e!r}")

    async def _main(self):
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
        mgr = await M.request_async()
        while True:
            try:
                await self._poll(mgr)
                t_end = time.time() + 0.5
                while time.time() < t_end:
                    try:
                        cmd = self.cmds.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.05)
                        continue
                    await self._command(mgr, cmd)
                    await self._poll(mgr)
            except Exception as e:
                core.log(f"media poll: {e!r}")
                await asyncio.sleep(1.0)

    @staticmethod
    def _pick(mgr):
        sessions = list(mgr.get_sessions())
        for s in sessions:  # Apple Music first
            if "applemusic" in (s.source_app_user_model_id or "").lower():
                return s
        return mgr.get_current_session() or (sessions[0] if sessions else None)

    async def _poll(self, mgr):
        s = self._pick(mgr)
        if s is None:
            self.active = self.playing = False
            self.can_seek = False
            self.position = self.duration = 0.0
            self.title = self.artist = self.album = self.source = ""
            if self.art is not None:
                self.art_version += 1
            self.art, self._key = None, None
            return
        self.active = True
        self.source = friendly_source(s.source_app_user_model_id)
        props = await s.try_get_media_properties_async()
        info = s.get_playback_info()
        tl = s.get_timeline_properties()
        self.playing = info.playback_status == 4  # PLAYING
        self.can_seek = bool(info.controls.is_playback_position_enabled)
        self.duration = max(0.0, (tl.end_time - tl.start_time).total_seconds())
        pos = tl.position.total_seconds()
        if self.playing and tl.last_updated_time:
            now = dt.datetime.now(dt.timezone.utc)
            pos += max(0.0, (now - tl.last_updated_time).total_seconds())
        self.position, self.stamp = pos, time.time()
        key = (props.title, props.artist, props.album_title)
        if key != self._key:
            self._key = key
            self.title, self.artist, self.album = props.title or "", props.artist or "", props.album_title or ""
            if " \u2014 " in self.artist and not self.album:
                self.artist, self.album = self.artist.split(" \u2014 ", 1)
            self.art = await self._thumbnail(props)
            self.art_version += 1

    @staticmethod
    async def _thumbnail(props):
        if props.thumbnail is None:
            return None
        try:
            from winrt.windows.storage.streams import Buffer, InputStreamOptions
            st = await props.thumbnail.open_read_async()
            buf = Buffer(st.size)
            await st.read_async(buf, st.size, InputStreamOptions.READ_AHEAD)
            return Image.open(io.BytesIO(bytes(buf))).convert("RGB")
        except Exception:
            return None

    async def _command(self, mgr, cmd):
        s = self._pick(mgr)
        if s is None:
            return
        if cmd == "toggle":
            await s.try_toggle_play_pause_async()
        elif cmd == "next":
            await s.try_skip_next_async()
        elif cmd == "previous":
            await s.try_skip_previous_async()
        elif isinstance(cmd, tuple) and cmd[0] == "seek":
            await s.try_change_playback_position_async(int(cmd[1] * 10_000_000))
