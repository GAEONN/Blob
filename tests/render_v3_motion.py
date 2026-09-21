"""Real shader: stationary cover with local visualizer, plus reactive bubble."""
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import numpy as np
from PIL import Image, ImageDraw
from test_regressions import blob, glass, fixtures, RecordingPanel


def render():
    snap, sound, media, am = fixtures()
    with patch.object(glass, 'ScreenSource', return_value=NS(frozen=False)):
        r = glass.GlassRenderer(1, 340, 400)
    r.set_supersample(2)
    r.cap[:] = (180, 180, 180, 255)
    for view in ('art', 'bubble'):
        frames = []
        for color, energy in (('white', 0), ('white', .9), ('black', 0), ('black', .9)):
            media.art = Image.new('RGB', (256,256), color)
            p = RecordingPanel(1)
            p.page, p.music_view, p.am, p.hover_key = 'music', view, am, 'arthover'
            p.draw(snap, sound, .35, False, media=media)
            r.set_content(p.ink, p.accent, p.pic, instant=True)
            a = blob.App.__new__(blob.App)
            a.panel, a.S, a.springs = p, 1, blob.Springs()
            a.pointer_style = 'system'
            a.attached = a.detaching = a.hover = a.pressed = a.slider_drag = None
            lenses, viz = a.resolve(p.controls, 1, '')
            for lens in lenses:
                lens['rect'] = tuple(v/2 for v in lens['rect'])
                for key in ('r','strength','bevel'):
                    if key in lens: lens[key] /= 2
            r.set_lenses(lenses)
            r.set_panel_shape(float(view=='bubble'))
            r.set_bubble_audio(energy,energy*.7,energy*.9)
            r.set_viz([v/2 for v in viz] if viz else None,
                      blob.music_visualizer_bands(np.zeros(28), energy), 1)
            height = round(p.height(snap)/2)
            with patch.object(glass.user32,'UpdateLayeredWindow',return_value=True):
                r.render(None,0,0,p.w/2,height,1,(-.55,-.83),.35)
            py, sp = r.panel_y(height), r.sp
            px = r.dib.arr[py-sp:py+height+sp].astype(np.float32)
            if view == 'art':
                if energy == 0:
                    rest_alpha = px[...,3].copy()
                else:
                    np.testing.assert_array_equal(rest_alpha, px[...,3],
                        err_msg='Audio must not move the outer cover silhouette')
            rgb = px[...,:3]+30*(1-px[...,3:4]/255)
            frames.append(Image.fromarray(np.clip(rgb[...,::-1],0,255).astype(np.uint8)))
        assert np.abs(np.asarray(frames[0],dtype=float)-np.asarray(frames[1],dtype=float)).sum()>10000
        sheet=Image.new('RGB',(r.W*4,frames[0].height+24),(30,30,30))
        d=ImageDraw.Draw(sheet)
        for i,frame in enumerate(frames):
            d.text((i*r.W+12,4),('White / rest','White / accent','Black / rest','Black / accent')[i], fill='white')
            sheet.paste(frame,(i*r.W,24))
        path=Path(__file__).resolve().parents[1]/f'v3-{view}-motion.tmp.png'
        sheet.save(path)
        print(path)

if __name__ == '__main__': render()
