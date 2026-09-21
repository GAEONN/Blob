"""Render real GPU glass against a synthetic desktop, without touching the live app.

python tests/render_preview.py --output <absolute-path.png>
"""
import argparse
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

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


def main(output, gaming=False, errors=False):
    snap, sound, media, am = fixtures()
    with patch.object(glass, "ScreenSource", return_value=NS(frozen=False)):
        renderer = glass.GlassRenderer(1, blob.Panel.GAMING_WIDE if gaming or errors else 340, 740)
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
             ("Settings / compact", True, "settings", "now", 10, False),
             ("Sound / conflict", False, "sound", "now", 0, True),
             ("Music / mini player", True, "music", "now", 0, False),
             ("Music / player bubble", True, "music", "bubble", 0, False),
             ("Artwork / hover controls", True, "music", "art", 0, False),
             ("Hardware / card", False, "blob", "now", 0, False),
             ("Hardware / details", False, "blob", "details", 0, False),
             ("Hardware / mode bubble", False, "blob", "bubble", 0, False),
             ("Search", True, "music", "search", 0, False),
             ("Playing Next", False, "music", "queue", 0, False)]
    if gaming:
        cases = [("Gaming / regular — synthetic data", False, "gaming", "now", 0, False),
                 ("Gaming / compact — synthetic data", True, "gaming", "now", 0, False),
                 ("Gaming / menu open — synthetic data", False, "gaming", "now", 1, False),
                 ("Gaming / no game focused", True, "gaming", "now", 2, False)]
    if errors:
        cases = [("Gaming / capture stopped", True, "gaming", "now", 0, False),
                 ("Queue / playback pending — synthetic track", False, "music", "queue", 0, False),
                 ("Queue / playback failure", True, "music", "queue", 1, False),
                 ("Sound / setup required", False, "sound", "now", 0, False)]
    for title, compact, page, view, scroll, conflict in cases:
        p = RecordingPanel(1)
        p.set_compact(compact)
        p.page, p.music_view, p.am, p.tabs_t = page, view, am, 1
        if page == "blob":
            p.tabs_t = 0
        p.hardware_view = "bubble" if page == "blob" and view == "bubble" else "card"
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
        app.S, app.panel, app.springs = 1, p, blob.Springs()
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
                  (page == "music" and p.music_view == "bubble"))
        renderer.set_panel_shape(1 if bubble else 0)
        renderer.set_bubble_pulse(.6 if page == "music" and p.music_view == "bubble" else 0)
        renderer.set_viz([v / p.SS for v in viz] if viz else None, np.linspace(.2, .9, 28), 1)
        height = round(p.height(snap) / p.SS)
        with patch.object(glass.user32, "UpdateLayeredWindow", return_value=True):
            renderer.render(None, 0, 0, p.w / p.SS, height, 1, (-.55, -.83), .35)
        py, sp = renderer.panel_y(height), renderer.sp
        pixels = renderer.dib.arr[py - sp:py + height + sp].astype(np.float32)
        rgb = pixels[..., :3] + 30 * (1 - pixels[..., 3:4] / 255)
        img = Image.fromarray(np.clip(rgb[..., ::-1], 0, 255).astype(np.uint8))
        cards.append((title, img))
    columns, cell_h = (1, 235) if gaming else (3, renderer.H + 24) if errors else (4, renderer.H + 24)
    sheet = Image.new("RGB", (columns * renderer.W, ((len(cards) + columns - 1) // columns) * cell_h), (30, 30, 30))
    d = ImageDraw.Draw(sheet)
    for i, (title, img) in enumerate(cards):
        x, y = (i % columns) * renderer.W, (i // columns) * cell_h
        d.text((x + 20, y + 8), title, fill="white")
        sheet.paste(img, (x, y + 24))
    sheet.save(output)
    print(Path(output).resolve())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--gaming", action="store_true")
    parser.add_argument("--errors", action="store_true")
    parser.add_argument("--morph", action="store_true")
    args = parser.parse_args()
    if args.morph:
        morph_preview(args.output)
    else:
        main(args.output, args.gaming, args.errors)
