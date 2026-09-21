"""Real GPU shader. Synthetic fixture artwork/telemetry, never production data."""
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import patch
import numpy as np
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_regressions import fixtures
import dashboard as dash


def render():
    out=Path(__file__).parent/'.impeccable'/'review';out.mkdir(parents=True,exist_ok=True)
    snap,sound,media,am=fixtures()
    for name,width,height,dock,expanded,settings,view in (
        ('overview',1120,740,False,False,False,'now'),
        ('small-display',900,620,False,False,False,'now'),
        ('full-display',1680,980,False,False,False,'now'),
        ('settings',1120,740,False,False,True,'now'),
        ('keyboard-focus',1120,740,False,False,True,'now'),
        ('queue',1120,740,False,False,False,'queue'),
        ('dock-bubble',92,480,True,False,False,'now'),
        ('dock-expanded',92,480,True,True,False,'now')):
        p=dash.DashboardPanel(1);p.window_w=width;p.window_h=height
        p.dock=dock;p.dock_open=expanded;p.settings_open=settings;p.music_view=view;p.am=am
        p.game=dict(fps=144,frame_ms=6.94)
        p.draw(snap,sound,.35,False,media=media)
        if name=='keyboard-focus':
            p.focus_key='slider:glass';p.draw_focus()
        with patch.object(dash.glass,'ScreenSource',return_value=NS(frozen=False)):
            r=dash.DashboardRenderer(1,width,height)
        r.set_supersample(2)
        # Muted photographic-like scene variation proves live-background refraction.
        yy,xx=np.indices(r.cap.shape[:2]);scene=np.clip(50+xx*.06+yy*.08,0,225)
        r.cap[...,0]=scene+8;r.cap[...,1]=scene+4;r.cap[...,2]=scene;r.cap[...,3]=255
        r.set_content(p.ink,p.accent,p.pic,instant=True)
        a=dash.DashboardApp.__new__(dash.DashboardApp)
        a.panel=p;a.S=1;a.springs=dash.base.Springs();a.pointer_style='system'
        a.attached=a.detaching=a.hover=a.pressed=a.slider_drag=None
        lenses,viz=a.resolve(p.controls,1,'')
        for lens in lenses:
            lens['rect']=tuple(v/2 for v in lens['rect'])
            for key in ('r','strength','bevel'):
                if key in lens:lens[key]/=2
        assert len(lenses)<=dash.glass.MAX_LENSES,(name,len(lenses))
        r.set_lenses(lenses);r.set_panel_shape(float(dock and not expanded))
        if viz:r.set_viz([v/2 for v in viz],np.linspace(.1,.6,28),1)
        ph=round(p.height()/2)
        with patch.object(dash.glass.user32,'UpdateLayeredWindow',return_value=True):
            r.render(None,0,0,p.w/2,ph,1,(-.55,-.83),.35)
        px=r.dib.arr[:ph+2*r.sp].astype(np.float32)
        rgb=px[...,:3]+35*(1-px[...,3:4]/255)
        path=out/(name+'.png')
        Image.fromarray(np.clip(rgb[...,::-1],0,255).astype(np.uint8)).save(path)
        print(path)
        r.dib.free();r.ctx.release()


if __name__=='__main__':render()
