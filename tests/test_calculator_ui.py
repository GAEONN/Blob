"""Calculator layout, monitor safety and real GPU previews; no desktop control."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import numpy as np
from PIL import Image, ImageDraw
from test_regressions import blob, glass, RecordingPanel, fixtures
from toolset import ToolController


class CalculatorLayoutTests(unittest.TestCase):
    def test_monitor_fit_leaves_other_view_adapters_alone(self):
        app = blob.App.__new__(blob.App)
        app.panel = NS(page="gaming", tabs_open=False)
        self.assertFalse(app.fit_calculator_in_work_area())

    def test_keyboard_editing_and_retraction_anchor(self):
        with tempfile.TemporaryDirectory() as folder:
            app = blob.App.__new__(blob.App)
            app.panel = RecordingPanel(1)
            app.panel.tools_open, app.panel.tool_view = True, "calculator"
            app.visible, app._overlay_unlocked, app.gaming_modifier_drag = True, True, False
            app.tools = ToolController(Path(folder)/"tools.json")
            app.draw_content = Mock()
            for char in "12*3\r":
                self.assertEqual(app.wndproc(123, 0x102, ord(char), 0), 0)
            self.assertEqual(app.tools.calculator.result, "36")
            app.tools.mode = "notes"
            for char in "x=2\rx*5":
                app.wndproc(123, 0x102, ord(char), 0)
            self.assertEqual(app.tools.note_results(), ["2", "10"])
            app.wndproc(123, blob.WM_KEYDOWN, 0x25, 0)
            app.wndproc(123, 0x102, ord("3"), 0)
            self.assertEqual(app.tools.notes, "x=2\nx*35")
            app.tool_top, app.hardware_top = 28, 540
            app._keep_tool_return_anchor()
            self.assertEqual(app.hardware_top, 28)

    def test_modes_history_and_popovers_fit_at_dpi_and_reduced_sizes(self):
        with tempfile.TemporaryDirectory() as folder:
            tools = ToolController(Path(folder)/"tools.json")
            tools.notes = "price=120\nprice*1.16"
            for dpi in (1, 1.25, 1.5, 2):
                for fit in (1, .8):
                    for mode in ("basic", "scientific", "notes", "convert"):
                        for overlay in (None, "mode", "history"):
                            p = RecordingPanel(dpi)
                            p.tools_open, p.tool_view, p.tool_size_scale = True, "calculator", fit
                            p.calculator_menu = "mode" if overlay == "mode" else None
                            p.calculator_history = overlay == "history"
                            tools.mode = mode
                            p.draw(*fixtures()[:2], .3, False, tools=tools)
                            for label, bounds in list(p.labels)+list(p.rects.items()):
                                self.assertGreaterEqual(bounds[0], 0, (mode, overlay, label, bounds))
                                self.assertGreaterEqual(bounds[1], 0, (mode, overlay, label, bounds))
                                self.assertLessEqual(bounds[2], p.w, (mode, overlay, label, bounds, p.w))
                                self.assertLessEqual(bounds[3], p.ink.height, (mode, overlay, label, bounds))
                            if overlay == "mode":
                                for choice in ("basic", "scientific", "notes", "convert"):
                                    box = p.rects["toolmode:"+choice]
                                    self.assertEqual(p.hit((box[0]+box[2])/2, (box[1]+box[3])/2), "toolmode:"+choice)

    def test_open_target_and_animation_stay_inside_work_area(self):
        for dpi in (1, 1.5, 2):
            for work in (NS(left=0, top=0, right=1280, bottom=680),
                         NS(left=-1920, top=-400, right=0, bottom=600)):
                app = blob.App.__new__(blob.App)
                app.panel = RecordingPanel(dpi)
                app.panel.tools_open, app.panel.tool_view = True, "calculator"
                app.S, app.ss, app.snap = dpi, 2, {}
                app.pos, app.tool_top = [work.right-70, work.bottom-100], 240*dpi
                app.glass = NS(sp=28*dpi, H=900*dpi, W=680*dpi,
                               panel_x=lambda width: 660*dpi-width)
                app.springs = blob.Springs()
                app.springs.get("width", app.panel.w)
                app.springs.get("height", app.panel.height({}))
                app._panel_screen_rect = lambda: (work.right-90, work.bottom-90, 80, 80)
                with patch.object(blob, "work_area_at", return_value=(work, None)):
                    app.fit_calculator_in_work_area()
                app.springs["width"].x = app.panel.w
                app.springs["height"].x = app.panel.height({})*1.03  # spring overshoot
                app._constrain_calculator_frame()
                width, height = app.springs["width"].x/2, app.springs["height"].x/2
                x, y = app.pos[0]+app.glass.panel_x(width), app.pos[1]+app.tool_top
                self.assertGreaterEqual(x-app.glass.sp, work.left-.01)
                self.assertGreaterEqual(y-app.glass.sp, work.top-.01)
                self.assertLessEqual(x+width+app.glass.sp, work.right+.01)
                self.assertLessEqual(y+height+app.glass.sp, work.bottom+.01)

    def test_all_scientific_key_lenses_fit_the_renderer_budget(self):
        renderer, frames, counts = render_calculator()
        self.assertTrue(all(count <= glass.MAX_LENSES for count in counts))
        self.assertGreater(counts[0], 48)  # regression: the old budget dropped bottom keys
        self.assertEqual(len(frames), 6)
        # Even when animating a short panel, lower keys must not render beyond it.
        with patch.object(glass.user32, "UpdateLayeredWindow", return_value=True):
            renderer.render(None, 0, 0, 420, 220, 1, (-.55, -.83), .2, panel_y=renderer.sp)
        self.assertLess(int(renderer.dib.arr[renderer.sp+400, renderer.sp+100, 3]), 2)


def render_calculator(output=None):
    with patch.object(glass, "ScreenSource", return_value=NS(frozen=False)):
        renderer = glass.GlassRenderer(1, 420, 748)
    renderer.set_supersample(2)
    renderer.set_tool_card(True)
    hh, ww = renderer.cap.shape[:2]
    yy, xx = np.mgrid[:hh, :ww]
    gray = np.where(xx < ww*.53, 45+yy*.02, 228-yy*.02).astype(np.uint8)
    renderer.cap[..., :3] = gray[..., None]
    renderer.cap[..., 3] = 255
    frames, counts = [], []
    with tempfile.TemporaryDirectory() as folder:
        tools = ToolController(Path(folder)/"tools.json")
        tools.calculator.expression, tools.calculator.result, tools.calculator.last_was_eval = "40000+71.38", "40071.38", True
        tools.notes = "price=120\nquantity=4\nprice*quantity\nprice*1.16"
        tools.history = [{"expression": f"{i}*12.5", "result": str(i*12.5), "time": 0} for i in range(1, 9)]
        variants = (("scientific", None), ("scientific", "mode"), ("basic", None),
                    ("notes", None), ("convert", None), ("scientific", "history"))
        for mode, overlay in variants:
            tools.mode = mode
            p = RecordingPanel(1)
            p.tools_open, p.tool_view = True, "calculator"
            p.calculator_menu = "mode" if overlay == "mode" else None
            p.calculator_history = overlay == "history"
            p.draw(*fixtures()[:2], .2, False, tools=tools)
            app = blob.App.__new__(blob.App)
            app.panel, app.S, app.springs = p, 1, blob.Springs()
            app.attached = app.detaching = app.hover = app.pressed = app.slider_drag = None
            app.pointer_style = "system"
            lenses, _ = app.resolve(p.controls, 1, "")
            counts.append(len(lenses))
            for lens in lenses:
                lens["rect"] = tuple(v/2 for v in lens["rect"])
                for key in ("r", "bevel", "strength"):
                    if key in lens:
                        lens[key] /= 2
            renderer.set_lenses(lenses)
            renderer.set_content(p.ink, p.accent, p.pic, instant=True)
            with patch.object(glass.user32, "UpdateLayeredWindow", return_value=True):
                renderer.render(None, 0, 0, 420, p.height({})/2, 1, (-.55, -.83), .2, panel_y=renderer.sp)
            frames.append(renderer.dib.arr.copy())
    if output:
        sheet = Image.new("RGB", (renderer.W*3, (renderer.H+25)*2), (30, 30, 30))
        for i, pixels in enumerate(frames):
            rgb = pixels[..., :3].astype(float)+30*(1-pixels[..., 3:4]/255)
            image = Image.fromarray(np.clip(rgb[..., ::-1], 0, 255).astype(np.uint8))
            x, y = i%3*renderer.W, i//3*(renderer.H+25)
            sheet.paste(image, (x, y+25))
            ImageDraw.Draw(sheet).text((x+16, y+7), str(variants[i])+" / synthetic preview", fill="white")
        sheet.save(output)
    return renderer, frames, counts


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--preview":
        render_calculator(sys.argv[2])
    else:
        unittest.main()
