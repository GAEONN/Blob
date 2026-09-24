"""Render real GPU glass against a synthetic desktop, without touching the live app.

python tests/render_preview.py --output <absolute-path.png>
"""
import argparse
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from test_regressions import blob, fixtures, glass, RecordingPanel


def morph_preview(output):
    """Contact sheet for the real shader's card-to-bubble interpolation."""
    snap, sound, media, am = fixtures()
    with patch.object(glass, "ScreenSource", return_value=NS(frozen=False)):
        renderer = glass.GlassRenderer(1, 340, 360)
    renderer.set_supersample(blob.Panel.SS)
    hh, ww = renderer.cap.shape[:2]
    yy, xx = np.mgrid[:hh, :ww]
    renderer.cap[..., 0] = 65 + (xx * 100 // ww)
    renderer.cap[..., 1] = 45 + (yy * 90 // hh)
    renderer.cap[..., 2] = 45 + (xx * 90 // ww)
    renderer.cap[..., 3] = 255

    panels = []
    for view in ("card", "bubble"):
        panel = RecordingPanel(1)
        panel.page, panel.am = "blob", am
        panel.hardware_view, panel.hardware_mode = view, "performance"
        panel.update_width()
        panel.draw(snap, sound, .35, False, media=media)
        panels.append(panel)
    card, bubble = panels
    renderer.set_content(card.ink, card.accent, card.pic, instant=True)
    renderer.set_content(bubble.ink, bubble.accent, bubble.pic)
    renderer.set_lenses([])

    steps = (0.0, .2, .4, .6, .8, 1.0)
    cell_h = 350
    anchored_top = renderer.panel_y(round(card.height(snap) / card.SS))
    sheet = Image.new("RGB", (len(steps) * renderer.W, cell_h), (30, 30, 30))
    draw = ImageDraw.Draw(sheet)
    for i, t in enumerate(steps):
        ease = t * t * (3 - 2 * t)
        width = (card.w + (bubble.w - card.w) * ease) / card.SS
        height = (card.height(snap) + (bubble.height(snap) - card.height(snap)) * ease) / card.SS
        renderer.set_panel_shape(t)
        with patch.object(glass.user32, "UpdateLayeredWindow", return_value=True):
            renderer.render(None, 0, 0, width, height, t, (-.55, -.83), .35,
                            panel_y=anchored_top)
        py, sp = anchored_top, renderer.sp
        pixels = renderer.dib.arr[py - sp:py + round(height) + sp].astype(np.float32)
        rgb = pixels[..., :3] + 30 * (1 - pixels[..., 3:4] / 255)
        image = Image.fromarray(np.clip(rgb[..., ::-1], 0, 255).astype(np.uint8))
        x = i * renderer.W
        draw.text((x + 12, 8), f"Morph {t:.1f}", fill="white")
        sheet.paste(image, (x, 28))
    sheet.save(output)
    print(Path(output).resolve())


def cover(image, width, height):
    """Scale and centre-crop an image so it fills width x height (CSS object-fit: cover)."""
    scale = max(width / image.width, height / image.height)
    size = (max(width, round(image.width * scale)), max(height, round(image.height * scale)))
    image = image.resize(size, Image.LANCZOS)
    left, top = (size[0] - width) // 2, (size[1] - height) // 2
    return image.crop((left, top, left + width, top + height))


def label(draw, xy, text, scale=1):
    """Caption that stays legible over a photo."""
    x, y = xy
    try:
        font = ImageFont.truetype("segoeui.ttf", round(12 * scale))
    except OSError:
        font = None
    box = draw.textbbox((x, y), text, font=font)
    draw.rounded_rectangle((box[0] - 6, box[1] - 4, box[2] + 6, box[3] + 4), 6, fill=(0, 0, 0, 150))
    draw.text((x, y), text, fill="white", font=font)


def main(output, gaming=False, errors=False, background=None, scale=1, album=False, cover_path=None):
    snap, sound, media, am = showcase_fixtures(cover_path) if album else fixtures()
    width = blob.Panel.GAMING_WIDE if gaming or errors else 340
    with patch.object(glass, "ScreenSource", return_value=NS(frozen=False)):
        renderer = glass.GlassRenderer(scale, round(width * scale), round(740 * scale))
    renderer.set_supersample(blob.Panel.SS)
    # A light/dark scene also exercises the actual per-pixel adaptive ink shader.
    hh, ww = renderer.cap.shape[:2]
    yy, xx = np.mgrid[:hh, :ww]
    bg = np.zeros((hh, ww, 4), np.uint8)
    bg[..., 0] = 65 + (xx * 100 // ww)
    bg[..., 1] = 45 + (yy * 90 // hh)
    bg[..., 2] = 45 + (xx * 90 // ww)
    bg[..., 3] = 255
    bg[(xx > ww * .65) & (yy < hh * .7), :3] = 235
    renderer.cap[:] = bg
    cards = []
    cases = [("Settings / regular", False, "settings", "now", 4, False),
             ("Settings / shortcut", True, "settings", "now", 17, False),
             ("Settings / compact", True, "settings", "now", 10, False),
             ("Sound / conflict", False, "sound", "now", 0, True),
             ("Music / mini player", True, "music", "now", 0, False),
             ("Music / regular full player", False, "music", "now", 0, False),
             ("Music / player bubble", True, "music", "bubble", 0, False),
             ("Artwork / hover controls", True, "music", "art", 0, False),
             ("Artwork / options panel", True, "music", "art", 0, False),
             ("Hardware / card", False, "blob", "now", 0, False),
             ("System / compact", True, "blob", "now", 0, False),
             ("Hardware / details", False, "blob", "details", 0, False),
             ("Hardware / mode bubble", False, "blob", "bubble", 0, False),
             ("Sound / status bubble", False, "sound", "bubble", 0, False),
             ("Search", True, "music", "search", 0, False),
             ("Playing Next", False, "music", "queue", 0, False)]
    if gaming:
        cases = [("Gaming / horizontal — synthetic data", False, "gaming", "now", 0, False),
                 ("Gaming / vertical — synthetic data", False, "gaming", "vertical", 0, False),
                 ("Gaming / FPS bubble", False, "gaming", "bubble", 0, False),
                 ("Gaming / unavailable bubble", False, "gaming", "bubble", 2, False),
                 ("Gaming / compact — synthetic data", True, "gaming", "now", 0, False),
                 ("Gaming / menu open — synthetic data", False, "gaming", "now", 1, False),
                 ("Gaming / no game focused", True, "gaming", "now", 2, False)]
    if errors:
        cases = [("Gaming / capture stopped", True, "gaming", "now", 0, False),
                 ("Queue / playback pending — synthetic track", False, "music", "queue", 0, False),
                 ("Queue / playback failure", True, "music", "queue", 1, False),
                 ("Sound / setup required", False, "sound", "now", 0, False)]
    caption = round(24 * scale)
    columns, cell_h = ((1, round(235 * scale)) if gaming else (3, renderer.H + caption) if errors
                       else (4, renderer.H + caption))
    sheet_w, sheet_h = columns * renderer.W, ((len(cases) + columns - 1) // columns) * cell_h
    desk = pad = None
    if background:
        # One continuous "desktop" behind the whole sheet: every card refracts, and is composited
        # over, the part of the photo it sits on. The padding covers the refraction margin.
        pad = renderer.H + renderer.M
        photo = cover(Image.open(background).convert("RGB"), sheet_w + 2 * pad, sheet_h + 2 * pad)
        desk = np.asarray(photo)[..., ::-1]  # BGR, like a desktop capture
    for title, compact, page, view, scroll, conflict in cases:
        p = RecordingPanel(scale)
        p.set_compact(compact)
        p.page, p.music_view, p.am, p.tabs_t = page, view, am, 1
        if page == "blob":
            p.tabs_t = 0
        p.hardware_view = "bubble" if page == "blob" and view == "bubble" else "card"
        p.sound_view = "bubble" if page == "sound" and view == "bubble" else "card"
        p.gaming_view = ("bubble" if page == "gaming" and view == "bubble" else
                         "vertical" if page == "gaming" and view == "vertical" else "horizontal")
        if title == "Music / volume panel":
            p.music_menu = "volume"
        elif title == "Artwork / options panel":
            p.music_menu = "options"
        p.hardware_mode = "balanced"
        p.update_width()
        if page == "music" and view == "art":
            p.hover_key = "arthover"
        if gaming:
            p.game = dict(fps=144, frame_ms=6.94, ram_percent=62, ram_gb=19.8)
            if scroll == 2:
                p.game.update(fps=None, frame_ms=None)
            p.tabs_open = scroll == 1
            p.tabs_t = float(p.tabs_open)
        if errors:
            p.game = dict(fps=None, frame_ms=None, ram_percent=62, ram_gb=19.8,
                          status="FPS unavailable. Capture stopped; retrying shortly.")
            p.tabs_open, p.tabs_t = False, 0
            am.queue_status = "Updating Playing Next…"
            am.queue_action_status = "Couldn't play. Try again." if scroll else "Playing ‘A very long song title’…"
            sound.cable = False if page == "sound" else True
        p.details_open = page == "blob" and view == "details"
        p.scroll["settings"] = scroll
        p.hw_note = "CPU, GPU and fan sensors come from HWiNFO shared memory."
        sound.fx_conflict = conflict
        p.draw(snap, sound, .35, False, media=media)
        renderer.set_content(p.ink, p.accent, p.pic, instant=True)
        app = blob.App.__new__(blob.App)
        app.S, app.panel, app.springs = scale, p, blob.Springs()
        app.attached = app.detaching = app.hover = app.pressed = app.slider_drag = None
        app.pointer_style = "system"
        lenses, viz = app.resolve(p.controls, 1, "")
        for lens in lenses:
            lens["rect"] = tuple(v / p.SS for v in lens["rect"])
            for key in ("r", "strength", "bevel"):
                if key in lens:
                    lens[key] /= p.SS
        renderer.set_lenses(lenses)
        bubble = ((page == "blob" and p.hardware_view == "bubble") or
                  (page == "music" and p.music_view == "bubble") or
                  (page == "sound" and p.sound_view == "bubble") or
                  (page == "gaming" and p.gaming_view == "bubble"))
        renderer.set_panel_shape(1 if bubble else 0)
        audio = (.85, .55, .9) if page == "music" and p.music_view == "bubble" else (0, 0, 0)
        renderer.set_bubble_audio(*audio)
        renderer.set_viz([v / p.SS for v in viz] if viz else None, np.linspace(.2, .9, 28), 1)
        height = round(p.height(snap) / p.SS)
        py, sp, M = renderer.panel_y(height), renderer.sp, renderer.M
        if desk is not None:
            i = len(cards)
            ox = (i % columns) * renderer.W + pad
            oy = (i // columns) * cell_h + caption - (py - sp) + pad  # window row py-sp lands under the caption
            ch, cw = renderer.cap.shape[:2]
            renderer.cap[..., :3] = desk[oy + py - M:oy + py - M + ch, ox + sp - M:ox + sp - M + cw]
            renderer.cap[..., 3] = 255
            renderer._bg_dirty = True
        with patch.object(glass.user32, "UpdateLayeredWindow", return_value=True):
            renderer.render(None, 0, 0, p.w / p.SS, height, 1, (-.55, -.83), .35)
        pixels = renderer.dib.arr[py - sp:py + height + sp].astype(np.float32)
        behind = 30 if desk is None else desk[oy + py - sp:oy + py + height + sp, ox:ox + renderer.W]
        rgb = pixels[..., :3] + behind * (1 - pixels[..., 3:4] / 255)
        img = Image.fromarray(np.clip(rgb[..., ::-1], 0, 255).astype(np.uint8))
        # Individual native-scale captures keep review text legible, unlike a giant contact sheet.
        destination = Path(output).parent / (Path(output).stem + "-" + str(len(cards)) + ".tmp.png")
        img.save(destination)
        cards.append((title, img))
    if desk is None:
        sheet = Image.new("RGB", (sheet_w, sheet_h), (30, 30, 30))
    else:
        sheet = Image.fromarray(np.ascontiguousarray(desk[pad:pad + sheet_h, pad:pad + sheet_w, ::-1]))
    d = ImageDraw.Draw(sheet, "RGBA")
    for i, (title, img) in enumerate(cards):
        x, y = (i % columns) * renderer.W, (i // columns) * cell_h
        sheet.paste(img, (x, y + caption))
        if desk is None:
            d.text((x + 20, y + 8), title, fill="white")
        else:
            label(d, (x + round(20 * scale), y + round(8 * scale)), title, scale)
    sheet.save(output)
    print(Path(output).resolve())


SHOWCASE_ALBUM = ("Drake", "Honestly, Nevermind", [
    "Intro", "Falling Back", "Texts Go Green", "Currents", "A Keeper", "Calling My Name", "Sticky",
    "Massive", "Flight's Booked", "Overdrive", "Down Hill", "Tie That Binds", "Liability",
    "Jimmy Cooks (feat. 21 Savage)"])


def placeholder_cover(size=1200):
    """Original abstract cover art (no third-party artwork): a warm glow on midnight blue."""
    yy, xx = np.mgrid[:size, :size] / size
    base = np.stack([.05 + .10 * yy, .07 + .08 * yy, .16 + .22 * (1 - xx * .5)], -1)
    glow = np.exp(-(((xx - .62) ** 2) / .045 + ((yy - .42) ** 2) / .035))[..., None]
    glow2 = np.exp(-(((xx - .30) ** 2) / .02 + ((yy - .72) ** 2) / .05))[..., None]
    ring = np.exp(-((np.hypot(xx - .5, yy - .5) - .31) ** 2) / .00012)[..., None]
    rgb = base + glow * np.array([1.0, .45, .22]) * .95 + glow2 * np.array([.55, .32, .95]) * .6 + ring * .35
    noise = np.random.default_rng(7).normal(0, .018, rgb.shape)
    return Image.fromarray((np.clip(rgb + noise, 0, 1) * 255).astype(np.uint8))


def showcase_fixtures(cover_path=None, now_playing=1):
    """Sample data for the README showcase: a real album's track list playing in Blob."""
    snap, sound, _media, am = fixtures()
    artist, album, tracks = SHOWCASE_ALBUM
    art = Image.open(cover_path).convert("RGB") if cover_path else placeholder_cover()
    media = NS(active=True, title=tracks[now_playing], artist=artist, album=album, source="Apple Music",
               playing=True, duration=265, pos_now=lambda: 74, art=art)
    am.queue = [dict(title=t, artist=artist, album=album, art=art) for t in tracks[now_playing + 1:]]
    am.results = [dict(title=t, artist=artist, album=album, art=art) for t in tracks]
    am.status = am.queue_status = ""
    snap["sensors"] = [dict(name=n, value=v, unit="°C", temp=True) for n, v in
                       (("CPU package", 64), ("GPU core", 58), ("NVMe SSD", 41), ("Motherboard", 38))]
    sound.output = "Speakers (Realtek Audio)"
    sound.spectrum = np.linspace(.85, .25, 28)
    return snap, sound, media, am


def render_panel(renderer, p, snap, sound, media, desk, x, y, scale, bubble=False, audio=(0, 0, 0)):
    """Render one real panel whose top-left sits at desk (x, y); returns (image, paste origin)."""
    p.update_width()
    p.draw(snap, sound, .35, False, media=media)
    renderer.set_content(p.ink, p.accent, p.pic, instant=True)
    app = blob.App.__new__(blob.App)
    app.S, app.panel, app.springs = scale, p, blob.Springs()
    app.attached = app.detaching = app.hover = app.pressed = app.slider_drag = None
    app.pointer_style = "system"
    lenses, viz = app.resolve(p.controls, 1, "")
    for lens in lenses:
        lens["rect"] = tuple(v / p.SS for v in lens["rect"])
        for key in ("r", "strength", "bevel"):
            if key in lens:
                lens[key] /= p.SS
    renderer.set_lenses(lenses)
    renderer.set_panel_shape(1 if bubble else 0)
    renderer.set_bubble_audio(*audio)
    renderer.set_viz([v / p.SS for v in viz] if viz else None, np.linspace(.2, .9, 28), 1)
    width, height = p.w / p.SS, round(p.height(snap) / p.SS)
    px, py, sp, M = renderer.panel_x(width), renderer.panel_y(height), renderer.sp, renderer.M
    ox, oy = x - px, y - py                     # desk position of the window's top-left pixel
    ch, cw = renderer.cap.shape[:2]
    renderer.cap[..., :3] = desk[oy + py - M:oy + py - M + ch, ox + sp - M:ox + sp - M + cw]
    renderer.cap[..., 3] = 255
    renderer._bg_dirty = True
    with patch.object(glass.user32, "UpdateLayeredWindow", return_value=True):
        renderer.render(None, 0, 0, width, height, 1, (-.55, -.83), .35)
    x0, x1 = px - sp, px + round(width) + sp
    pixels = renderer.dib.arr[py - sp:py + height + sp, x0:x1].astype(np.float32)
    behind = desk[oy + py - sp:oy + py + height + sp, ox + x0:ox + x1]
    rgb = pixels[..., :3] + behind * (1 - pixels[..., 3:4] / 255)
    return Image.fromarray(np.clip(rgb[..., ::-1], 0, 255).astype(np.uint8)), (x - sp, y - sp)


def showcase(output, background, cover=None, scale=2, size=(1920, 960)):
    """One desktop-sized hero shot: Music, Playing Next, System, Gaming and bubbles over a wallpaper."""
    snap, sound, media, am = showcase_fixtures(cover)
    S = scale
    cw, chh = round(size[0] * S), round(size[1] * S)
    pad = round(900 * S)
    photo = cover_image(background, cw + 2 * pad, chh + 2 * pad)
    desk = np.asarray(photo)[..., ::-1]
    def renderer(width, height):
        # Each renderer owns a standalone GL context and moderngl does not switch between them,
        # so create one only after the previous renderer is finished.
        with patch.object(glass, "ScreenSource", return_value=NS(frozen=False)):
            r = glass.GlassRenderer(S, round(width * S), round(height * S))
        r.set_supersample(blob.Panel.SS)
        return r

    def panel(page, view="now", compact=False, **extra):
        p = RecordingPanel(S)
        p.set_compact(compact)
        p.page, p.music_view, p.am, p.tabs_t = page, view, am, 0 if page == "blob" else 1
        p.hardware_view = "bubble" if page == "blob" and view == "bubble" else "card"
        p.sound_view = "bubble" if page == "sound" and view == "bubble" else "card"
        p.gaming_view = "horizontal"
        p.hardware_mode = "balanced"
        p.hw_note = "CPU, GPU and fan sensors come from LibreHardwareMonitor."
        for key, value in extra.items():
            setattr(p, key, value)
        return p

    L = lambda v: round(v * S)

    def panel_height(p):
        p.update_width()
        p.draw(snap, sound, .35, False, media=media)
        return round(p.height(snap) / p.SS)

    # Lay everything out first (heights are known before rendering), then render per renderer.
    music, queue, hw = panel("music"), panel("music", "queue"), panel("blob")
    game = panel("gaming", game=dict(fps=144, frame_ms=6.94, ram_percent=48, ram_gb=7.3),
                 tabs_open=False, tabs_t=0)
    bubbles = [(panel("music", "bubble", compact=True), (.85, .55, .9)),
               (panel("sound", "bubble"), (0, 0, 0)), (panel("blob", "bubble"), (0, 0, 0))]
    top, right_x = L(170), L(1060)
    game_y = top + panel_height(hw) + L(40)
    bubble_y = game_y + panel_height(game) + L(40)

    shots = []
    wide = renderer(340, 740)
    shots.append(render_panel(wide, music, snap, sound, media, desk, pad + L(250), pad + top, S))
    shots.append(render_panel(wide, queue, snap, sound, media, desk, pad + L(640), pad + top, S))
    shots.append(render_panel(wide, hw, snap, sound, media, desk, pad + right_x, pad + top, S))
    for i, (p, audio) in enumerate(bubbles):
        shots.append(render_panel(wide, p, snap, sound, media, desk, pad + right_x + L(140) * i,
                                  pad + bubble_y, S, bubble=True, audio=audio))
    strip = renderer(blob.Panel.GAMING_WIDE, 300)
    shots.append(render_panel(strip, game, snap, sound, media, desk, pad + right_x, pad + game_y, S))
    scene = Image.fromarray(np.ascontiguousarray(desk[..., ::-1]))
    for img, (x, y) in shots:
        scene.paste(img, (x, y))
    scene.crop((pad, pad, pad + cw, pad + chh)).save(output, optimize=True)
    print(Path(output).resolve())


def cover_image(path, width, height):
    return cover(Image.open(path).convert("RGB"), width, height)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--gaming", action="store_true")
    parser.add_argument("--errors", action="store_true")
    parser.add_argument("--morph", action="store_true")
    parser.add_argument("--background", help="photo to use as the desktop behind the glass")
    parser.add_argument("--scale", type=float, default=1, help="display scale, e.g. 2 for a HiDPI render")
    parser.add_argument("--showcase", action="store_true", help="one desktop-sized hero scene (needs --background)")
    parser.add_argument("--cover", help="album artwork for --showcase/--album (default: an original placeholder)")
    parser.add_argument("--album", action="store_true", help="use the showcase album instead of stress-test data")
    args = parser.parse_args()
    if args.morph:
        morph_preview(args.output)
    elif args.showcase:
        showcase(args.output, args.background, args.cover, args.scale if args.scale != 1 else 2)
    else:
        main(args.output, args.gaming, args.errors, args.background, args.scale, args.album, args.cover)
