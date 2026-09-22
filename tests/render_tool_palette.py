"""Offscreen GPU preview of the radial palette; never controls the live desktop."""
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import argparse
import numpy as np
from PIL import Image, ImageDraw
from test_regressions import fixtures, RecordingPanel, blob, glass


def render(output):
    snap, sound, media, _ = fixtures()
    with patch.object(glass, "ScreenSource", return_value=NS(frozen=False)):
        renderer = glass.GlassRenderer(1, 240, 260)
    renderer.set_supersample(blob.Panel.SS)
    hh, ww = renderer.cap.shape[:2]
    yy, xx = np.mgrid[:hh, :ww]
    renderer.cap[..., :3] = np.where(((xx // 42 + yy // 42) % 2)[..., None],
                                     [114, 106, 85], [62, 57, 49])
    renderer.cap[..., 3] = 255
    sheet = Image.new("RGB", (renderer.W * 3, 460), (30, 30, 30))
    draw = ImageDraw.Draw(sheet)
    for row, side in enumerate(("right", "left")):
        for column, reveal in enumerate((0.0, .55, 1.0)):
            panel = RecordingPanel(1)
            panel.page, panel.hardware_view = "blob", "bubble"
            panel.anchor_side, panel.tool_reveal = side, reveal
            panel.draw(snap, sound, .12, False, media=media)
            renderer.set_content(panel.ink, panel.accent, panel.pic, instant=True)
            app = blob.App.__new__(blob.App)
            app.S, app.panel, app.springs = 1, panel, blob.Springs()
            app.attached = app.detaching = app.hover = app.pressed = app.slider_drag = None
            app.pointer_style = "system"
            lenses, _ = app.resolve(panel.controls, 1, "")
            for lens in lenses:
                lens["rect"] = tuple(v / panel.SS for v in lens["rect"])
                for key in ("r", "strength", "bevel"):
                    if key in lens:
                        lens[key] /= panel.SS
            renderer.set_lenses(lenses)
            renderer.set_panel_side(side)
            renderer.set_panel_shape(1)
            renderer.set_bubble_proximity(reveal)
            renderer.set_tool_expansion(float(reveal > .06))
            height = panel.height(snap) / panel.SS
            with patch.object(glass.user32, "UpdateLayeredWindow", return_value=True):
                renderer.render(None, 0, 0, panel.w / panel.SS, height,
                                1, (-.55, -.83), .12, panel_y=renderer.sp)
            pixels = renderer.dib.arr[:195].astype(np.float32)
            rgb = pixels[..., :3] + 30 * (1 - pixels[..., 3:4] / 255)
            img = Image.fromarray(np.clip(rgb[..., ::-1], 0, 255).astype(np.uint8))
            x, y = column * renderer.W, row * 230
            draw.text((x + 12, y + 6), f"{side} / reveal {reveal:.2f} / synthetic readings", fill="white")
            sheet.paste(img, (x, y + 24))
    sheet.save(output)
    print(Path(output).resolve())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    render(parser.parse_args().output)
