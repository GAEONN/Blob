"""Separate full-screen companion for Blob. Never edits SmallBlob's source or settings."""
import ctypes
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
import time
import winreg

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
loader = importlib.machinery.SourceFileLoader('blob_dashboard_base', str(ROOT / 'blob.pyw'))
spec = importlib.util.spec_from_loader(loader.name, loader)
base = importlib.util.module_from_spec(spec)
loader.exec_module(base)
glass, engine, user32 = base.glass, base.engine, base.user32
BasePanel, BaseRenderer = base.Panel, glass.GlassRenderer


class DashboardPanel(BasePanel):
    WIDE = GAMING_WIDE = 1120
    DASH_H = 740
    DOCK_W = 92
    DOCK_H = 480
    BUBBLE_HEIGHT = 92 * 98 / 104

    def __init__(self, scale):
        self.dock = False
        self.dock_open = False
        self.settings_open = False
        self.fullscreen = True
        self.window_w, self.window_h = self.WIDE, self.DASH_H
        super().__init__(scale)
        self.labels = []
        self.regions = {}
        self.focus_key = None
        self.page = 'music'
        self.hotkey_label = base.DUAL_CONTROL_LABEL

    def set_compact(self, compact):
        # SmallBlob size preferences never apply to this surface.
        self.compact = False
        self.update_width()

    def update_width(self):
        self.w = round((self.DOCK_W if self.dock else self.window_w) * self.S)

    def height(self, s=None, page=None):
        return round((self.DOCK_H if self.dock_open else self.BUBBLE_HEIGHT) * self.S) if self.dock else round(self.window_h*self.S)

    def max_height(self):
        return round(max(self.DASH_H, self.DOCK_H) * self.S)

    def label(self, x, y, text, size, style='Regular', a=205, anchor='la', temp=None, icon=False):
        font = self.f.icon(size) if icon else self.text_font(text, size, style)
        self.labels.append((text, self.di.textbbox((x, y), text, font=font, anchor=anchor)))
        # Hierarchy comes from size/weight; transparent ink vanishes on middle grays.
        if temp is not None and temp >= 80:
            bounds=self.di.textbbox((x,y),text,font=font,anchor=anchor)
            # Readable adaptive numerals, plus an amber/red status mark with its own
            # dark backing. Warning hue never becomes low-contrast numeral ink.
            cx,cy=bounds[2]+9*self.S,(bounds[1]+bounds[3])/2
            rr=4*self.S
            self.da.ellipse((cx-rr,cy-rr,cx+rr,cy+rr),fill=(20,20,20,255))
            rr=2.8*self.S
            self.da.ellipse((cx-rr,cy-rr,cx+rr,cy+rr),fill=base.HOT if temp>=90 else base.WARN)
        return super().label(x, y, text, size, style, 255, anchor, None, icon)

    def draw_focus(self):
        focus=self.rects.get(self.focus_key)
        if focus:
            x0,y0,x1,y1=map(round,focus)
            self.di.rounded_rectangle((x0+2,y0+2,x1-2,y1-2),
                radius=min(12*self.S,(y1-y0)/2),outline=255,width=max(1,round(self.S)))

    def card(self, name, box):
        self.regions[name] = box
        # 34-DIP outer edge - 18-DIP inset = 16-DIP inner edge.
        self.static(box, 16*self.S, strength=6*self.S, bevel=14*self.S,
                    rim=.55, frost=.82, lift=.06, n=glass.SQUIRCLE_N if hasattr(glass, 'SQUIRCLE_N') else 2.2)

    def button(self, key, text, box, selected=False):
        x0,y0,x1,y1 = box
        self.hover_lens(key, box, (y1-y0)/2)
        if selected:
            self.static(box, (y1-y0)/2, strength=4*self.S, bevel=8*self.S,
                        rim=.6, frost=1, lift=.14, raised=1)
        self.label((x0+x1)/2,(y0+y1)/2,text,12,'Semibold Text' if selected else 'Regular',255,'mm')

    def draw(self, s, snd, glassiness, startup, pointer='system', media=None, seek=None, captureable=False):
        self.update_width()
        self._begin(self.height(s))
        self.labels, self.regions = [], {}
        s = s or base.empty_snapshot()
        if self.dock:
            self.draw_dock(s)
            return self.ink, self.accent, self.controls
        S, W, H = self.S, self.w, self.height(s)
        self.label(26*S,24*S,'Blob',25,'Semibold Display',255)
        self.label(95*S,34*S,'Overview',13,'Regular',205)
        self.button('dash:settings','Settings',(W-310*S,22*S,W-228*S,54*S),self.settings_open)
        self.button('dash:dock','Game dock',(W-218*S,22*S,W-116*S,54*S))
        self.glass_button('dash:window',W-80*S,38*S,16*S,'\uE923' if self.fullscreen else '\uE922',10)
        self.glass_button('dash:close',W-38*S,38*S,16*S,'\uE8BB',10)
        top, bottom, gap, inset = 78*S,H-18*S,16*S,18*S
        rail = (inset,top,inset+96*S,bottom)
        left = rail[2]+gap
        avail = W-inset-left-gap
        music_w = max(320*S, avail*.57)
        music_box = (left,top,left+music_w,bottom)
        right = (music_box[2]+gap,top,W-inset,bottom)
        self.card('gaming',rail)
        self.gaming_rail(s,rail)
        self.card('music',music_box)
        if self.music_view in ('search','queue'):
            self.music_list(music_box)
        else:
            self.music_player(media,snd,seek,music_box)
        if self.settings_open:
            self.card('settings',right)
            self.settings_panel(glassiness,captureable,right)
        else:
            rh = right[3]-right[1]
            split = right[1]+min(270*S,rh*.47)
            system_box=(right[0],right[1],right[2],split)
            sound_box=(right[0],split+gap,right[2],right[3])
            self.card('system',system_box)
            self.card('sound',sound_box)
            self.system_panel(s,system_box)
            self.sound_panel(snd,sound_box)
        return self.ink,self.accent,self.controls

    def music_player(self,m,snd,seek,box):
        S=self.S
        x0,y0,x1,y1=box
        width=x1-x0
        full=self.music_view=='art'
        self.label(x0+20*S,y0+18*S,'Music',16,'Semibold Text',255)
        self.glass_button('mview:search',x1-64*S,y0+29*S,15*S,'\uE721',11)
        self.glass_button('mview:queue',x1-25*S,y0+29*S,15*S,'\uE8FD',11)
        a=round(min(width-40*S,(y1-y0)-(218 if full else 254)*S))
        a=max(round(128*S),a)
        ax=round((x0+x1-a)/2); ay=round(y0+62*S)
        self._artwork(m,ax,ay,a,8*S,36)
        self.regions['album']=(ax,ay,ax+a,ay+a)
        ty=ay+a+18*S
        self.label((x0+x1)/2,ty,self.fit(m.title if m and m.active else 'Not playing',18,'Semibold Text',width-44*S),18,'Semibold Text',255,'ma')
        self.label((x0+x1)/2,ty+26*S,self.fit((m.artist or m.source) if m and m.active else 'Open search to choose music',13,'Regular',width-44*S),13,'Regular',210,'ma')
        self._progress(m,seek,x0+34*S,x1-34*S,ty+62*S,times=False)
        cy=ty+110*S; cx=(x0+x1)/2
        self.transport('am:shuffle',x0+36*S,cy,15*S,'shuffle',active=bool(getattr(self.am,'shuffle',False)))
        self.transport('media:previous',cx-62*S,cy,20*S,'previous')
        self.transport('media:toggle',cx,cy,28*S,'pause' if m and m.playing else 'play',always=True)
        self.transport('media:next',cx+62*S,cy,20*S,'next')
        repeat=getattr(self.am,'repeat','off')
        self.transport('am:repeat',x1-36*S,cy,15*S,'repeat_one' if repeat=='one' else 'repeat',active=repeat!='off')
        if not full:
            self.label(x0+24*S,y1-31*S,'Volume',11,'Regular',205,'lm')
            self.slider('volume',snd.volume if snd.volume is not None else .5,x0+92*S,x1-34*S,y1-31*S)
        if self.options.get('musicreactive',True):
            vcx=ax+a/2; half=min(120*S,a/2-18*S)
            self.controls.append(('viz',(vcx-half,ay+a-38*S,vcx+half,ay+a-12*S)))

    def music_list(self,box):
        # Reuse the real search / queue implementation, including artwork and states.
        x0,y0,x1,y1=map(round,box)
        child=BasePanel(self.S/self.SS)
        child.page='music';child.music_view=self.music_view
        child.w=x1-x0
        child.am=self.am;child.query=self.query;child.scroll=self.scroll
        child.options=self.options
        child.inner_radius=lambda inset,cap=None:min(6*self.S,cap if cap is not None else 6*self.S)
        child_label=child.label
        child.label=lambda x,y,t,size,style='Regular',a=175,anchor='la',temp=None,icon=False: child_label(x,y,t,size,style,255,anchor,None,icon)
        child._begin(y1-y0)
        child.height=lambda s=None,page=None:y1-y0
        if self.music_view=='search':child._music_search(20*self.S,14*self.S)
        else:child._music_queue(20*self.S,14*self.S)
        self.ink.paste(child.ink,(x0,y0));self.accent.alpha_composite(child.accent,(x0,y0));self.pic.alpha_composite(child.pic,(x0,y0))
        shift=lambda r:(r[0]+x0,r[1]+y0,r[2]+x0,r[3]+y0)
        for key,rect in child.rects.items():self.rects[key]=shift(rect)
        for c in child.controls:
            if c[0]=='static':self.controls.append(('static',dict(c[1],rect=shift(c[1]['rect']))))
            elif c[0]=='hover':self.controls.append(('hover',c[1],shift(c[2]),c[3]))

    def system_panel(self,s,box):
        S=self.S;x0,y0,x1,y1=box;w=x1-x0
        self.label(x0+20*S,y0+18*S,'System',16,'Semibold Text',255)
        for i,key in enumerate(('cpu','gpu')):
            item=s.get(key,{})
            x=x0+20*S+i*(w-40*S)/2
            self.label(x,y0+62*S,key.upper(),12,'Regular',210)
            self.label(x,y0+84*S,base.fmt_temp(item.get('temp')),32,'Semibold Display',255,temp=item.get('temp'))
            load=item.get('load')
            hint=f'{load:.0f}% usage' if load is not None else 'No load sensor'
            self.label(x,y0+130*S,self.fit(hint,11,'Regular',(w-48*S)/2),11,'Regular',205)
        fans=s.get('fans',[])
        fy=y0+166*S
        if fans:
            for i,fan in enumerate(fans[:2]):
                rpm=fan.get('rpm'); name=fan.get('name') or f'Fan {i+1}'
                self.label(x0+20*S,fy+i*25*S,self.fit(name,12,'Regular',w-145*S),12,'Regular',215)
                self.label(x1-20*S,fy+i*25*S,f'{rpm:,.0f} rpm' if rpm is not None else '— rpm',12,'Semibold Text',255,'ra')
        else:
            self.label(x0+20*S,fy,'Fan RPM unavailable',12,'Regular',205)
        power=s.get('power',{});battery=power.get('battery')
        text=('Plugged in' if power.get('plugged') else 'Battery')+(f' · {battery:.0f}%' if battery is not None else '')
        if y1-fy>70*S:self.label(x0+20*S,y1-30*S,text,11,'Regular',205)

    def sound_panel(self,snd,box):
        S=self.S;x0,y0,x1,y1=box;w=x1-x0
        self.label(x0+20*S,y0+18*S,'Sound',16,'Semibold Text',255)
        self.toggle('sound',snd.enabled,x1-68*S,y0+16*S)
        status=(getattr(snd,'error','') or ('Boost on' if snd.enabled else 'Boost off'))
        if getattr(snd,'fx_conflict',False):status='FxSound is active — close it before enabling boost'
        if getattr(snd,'cable',None) is False:status='Audio setup needed in SmallBlob'
        self.label(x0+20*S,y0+51*S,self.fit(status,11,'Regular',w-40*S),11,'Regular',210)
        for i,(key,name) in enumerate((('boost','Boost'),('bass','Bass'),('clarity','Clarity'))):
            y=y0+(88+i*57)*S
            value=getattr(snd,key,0)
            self.label(x0+20*S,y,name,12,'Regular',230)
            self.label(x1-20*S,y,f'{round(value*100)}%',12,'Semibold Text',255,'ra')
            self.slider(key,value,x0+33*S,x1-33*S,y+29*S)
        if y1-y0>310*S:
            self.label(x0+20*S,y1-31*S,self.fit(snd.output or 'Default output',11,'Regular',w-40*S),11,'Regular',205)

    def gaming_rail(self,s,box):
        S=self.S;x0,y0,x1,y1=box;cx=(x0+x1)/2
        self.label(cx,y0+22*S,'Gaming',12,'Semibold Text',255,'ma')
        self.gaming_values(s,cx,y0+63*S,step=94*S,detail=True)
        self.button('dash:dock','Dock',(x0+10*S,y1-48*S,x1-10*S,y1-14*S))

    def gaming_values(self,s,cx,top,step,detail=False):
        S=self.S;g=self.game;cpu=s.get('cpu',{});gpu=s.get('gpu',{})
        fps,ms=g.get('fps'),g.get('frame_ms')
        items=[('FPS',f'{fps:.0f}' if fps is not None else '—',f'{ms:.1f} ms' if ms is not None else '— ms'),
               ('CPU',base.fmt_temp(cpu.get('temp')),''),
               ('GPU',base.fmt_temp(gpu.get('temp')),''),
               ('POWER',f"{gpu['power']:.0f} W" if gpu.get('power') is not None else '— W','')]
        for i,(name,value,hint) in enumerate(items):
            y=top+i*step
            self.label(cx,y,name,10,'Semibold Text',205,'ma')
            temp=cpu.get('temp') if name=='CPU' else gpu.get('temp') if name=='GPU' else None
            self.label(cx,y+17*S,value,25 if i==0 else 22,'Semibold Display',255,'ma',temp=temp)
            if hint:self.label(cx,y+49*S,hint,11,'Regular',215,'ma')

    def draw_dock(self,s):
        S=self.S;W=self.w
        if not self.dock_open:
            u=self.DOCK_W/104
            fps,ms=self.game.get('fps'),self.game.get('frame_ms')
            self.label(43*u*S,39*u*S,f'{fps:.0f}' if fps is not None else '—',22,'Semibold Display',255,'mm')
            self.label(43*u*S,61*u*S,'FPS',9,'Semibold Text',205,'mm')
            self.label(43*u*S,79*u*S,f'{ms:.1f} ms' if ms is not None else '— ms',10,'Regular',225,'mm')
            self.hover_lens('dock:expand',(58*u*S,u*S,101*u*S,44*u*S),21.5*u*S)
            self.rects['dock:dashboard']=(5*S,26*S,64*S,83*S)
            return
        self.glass_button('dock:collapse',W/2,26*S,14*S,'\uE70E',10)
        self.gaming_values(s,W/2,58*S,86*S)
        self.glass_button('dock:lock',W/2,411*S,15*S,'\uE72E',11)
        self.button('dock:dashboard','Blob',(12*S,440*S,W-12*S,469*S))

    def settings_panel(self,glassiness,captureable,box):
        S=self.S;x0,y0,x1,y1=box
        self.label(x0+20*S,y0+18*S,'Settings',16,'Semibold Text',255)
        self.label(x0+20*S,y0+66*S,'Glass',13,'Semibold Text',255)
        self.slider('glass',glassiness,x0+34*S,x1-34*S,y0+108*S)
        for i,(key,title,hint,value) in enumerate((
            ('musicreactive','Music visualizer','Bars inside artwork',self.options.get('musicreactive',True)),
            ('capture','Include in screenshots','Freezes glass to prevent feedback',captureable))):
            y=y0+(148+i*100)*S
            for j,line in enumerate(self.wrap(title,13,'Semibold Text',x1-x0-105*S)):
                self.label(x0+20*S,y+j*18*S,line,13,'Semibold Text',255)
            self.toggle(key,value,x1-68*S,y-2*S)
            for j,line in enumerate(self.wrap(hint,11,'Regular',x1-x0-40*S)):
                self.label(x0+20*S,y+40*S+j*15*S,line,11,'Regular',215)
        self.label(x0+20*S,y0+369*S,'Game dock lock',13,'Semibold Text',255)
        self.static((x0+20*S,y0+402*S,x1-20*S,y0+438*S),15*S,strength=5*S,bevel=8*S,
                    rim=.8,frost=1.0,lift=.10,raised=.7)
        self.label((x0+x1)/2,y0+420*S,getattr(self,'hotkey_label',base.DUAL_CONTROL_LABEL),11,
                   'Semibold Text',255,'mm')
        self.label(x0+20*S,y0+455*S,'Locks or unlocks Blob in every view.',11,'Regular',205)
        self.label(x0+20*S,y1-32*S,getattr(self,'settings_note','SmallBlob settings stay separate.'),11,'Regular',210)


class DashboardRenderer(BaseRenderer):
    def __init__(self,*args,**kwargs):
        # Process-local shader variant. SmallBlob's renderer file stays untouched.
        old=glass.FRAG
        glass.FRAG=old.replace('vec2 q = vec2(pp.x, pp.y - (panel_size.y - size.y / ink_ss)) * ink_ss;',
                               'vec2 q = pp * ink_ss;')
        glass.FRAG=glass.FRAG.replace(
            'float ink_dark = mix(dark, smoothstep(.45, .65, dot(col, vec3(.2126, .7152, .0722))), pic.a);',
            'vec3 ic = clamp(col, 0.0, 1.0);\n'
            'vec3 linear_ink = mix(ic / 12.92, pow((ic + .055) / 1.055, vec3(2.4)), step(vec3(.04045), ic));\n'
            'float ink_dark = step(.179, dot(linear_ink, vec3(.2126, .7152, .0722)));')
        glass.FRAG=glass.FRAG.replace('vec3 ink_col = mix(vec3(1.0), vec3(0.07), ink_dark);',
                                     'vec3 ink_col = mix(vec3(1.0), vec3(0.0), ink_dark);')
        try:super().__init__(*args,**kwargs)
        finally:glass.FRAG=old

    def panel_x(self,w):return self.sp
    def panel_y(self,h):return self.sp

    def resize_surface(self,width,height):
        if (self.w,self.hmax)==(width,height):return
        self.w,self.hmax=width,height
        self.W,self.H=width+2*self.sp,height+2*self.sp
        self.dib.free();self.fbo.release();self.bg.release()
        self.dib=glass.Dib(self.W,self.H)
        self.cap=np.zeros((height+2*self.M,width+2*self.M,4),np.uint8)
        self.fbo=self.ctx.simple_framebuffer((self.W,self.H),components=4)
        self.bg=self.ctx.texture((self.cap.shape[1],self.cap.shape[0]),4,dtype='f1')
        self.bg.build_mipmaps();self.bg.filter=(self.ctx.LINEAR_MIPMAP_LINEAR,self.ctx.LINEAR)
        self.bg.repeat_x=self.bg.repeat_y=False
        self.prog['bg_size'].value=(self.cap.shape[1],self.cap.shape[0])
        self._last_key=None


class DashboardApp(base.App):
    def __init__(self):
        self.dashboard_pos=None
        self.last_dashboard_text=0
        super().__init__()

    def _panel_y(self,height):return self.glass.sp

    @property
    def bubble_mode(self):return self.panel.dock and not self.panel.dock_open

    @property
    def overlay_lock_available(self):return True

    def toggle_overlay_input(self):
        # The v4 lock is global: the same chord must work in the overview,
        # Music, Settings, and the detached game dock.
        super().toggle_overlay_input()

    def bubble_drag_key(self,key):return False

    @property
    def bubble_drag_active(self):return False

    def _schedule(self,animating=True):
        # The full-screen compositor is capped at 30 Hz; narrow dock at 60 Hz.
        want=16 if self.panel.dock else 33
        if self.interval!=want:
            self.interval=want;user32.SetTimer(self.hwnd,1,want,None)

    def show(self):
        work,_=base.work_area_at(*base.cursor_pos())
        if self.pos is None:
            self.pos=[work.left+round(12*self.S)-self.glass.sp,
                      work.top+round(12*self.S)-self.glass.sp]
        super().show()

    def apply_gaming_input(self):
        super().apply_gaming_input()
        # The dashboard is a normal app; only its detached dock stays above games.
        style=user32.GetWindowLongPtrW(self.hwnd,-20)
        if self.panel.dock:
            style=(style|0x80)&~0x40000
        elif self.overlay_locked:
            style=(style|0x80|0x08000000)&~0x40000
        else:
            style=(style|0x40000)&~0x80
        user32.SetWindowLongPtrW(self.hwnd,-20,style)
        user32.SetWindowPos(self.hwnd,-1 if self.panel.dock else -2,0,0,0,0,0x13)

    def _draw_content(self,crossfade=False):
        self.gaming.set_active(self.visible)
        self.panel.game=self.gaming.snapshot()
        self.panel.hover_key=self.hover
        self.panel.hotkey_editing=self.hotkey_editing
        ink,accent,controls=self.panel.draw(self.snap,self.sound,self.glassiness,self.startup,
                                         'system',self.media,self.seek_value,self.captureable)
        if self.overlay_unlocked:self.panel.draw_focus()
        if crossfade:
            self.old_controls=self.controls
            self.glass.set_content(ink,accent,self.panel.pic)
            f=self.springs.get('fade',1,k=190,zeta=1)
            f.x,f.v,f.target=0,0,1
        else:self.glass.replace_content(ink,accent,self.panel.pic)
        self.controls=controls
        for name,value in (('height',self.panel.height()),('width',self.panel.w)):
            self.springs.get(name,value,k=230,zeta=.94).target=value
        self.frame_dirty=True

    def _frame(self):
        now=time.perf_counter()
        # Mouse messages also enter frame(); enforce the same budget there.
        if now-self.last_frame < (1/60 if self.panel.dock else 1/30):return
        self.update_gaming_modifier_drag()
        dt=min(.05,now-self.last_frame);self.last_frame=now
        self.springs.step(dt)
        fade=self.springs.get('fade',1).x
        width=self.springs.get('width',self.panel.w)
        height=self.springs.get('height',self.panel.height())
        morph=self.springs.get('dashboard_dock',0,k=230,zeta=.94)
        morph.target=float(self.panel.dock and not self.panel.dock_open)
        self.sound.heartbeat()
        self.sound.set_reactive_active(not self.panel.dock and self.panel.options.get('musicreactive',True))
        if now-self.last_dashboard_text > .5 and not self.slider_drag:
            self.last_dashboard_text=now
            self.draw_content()
        if not self.panel.dock and self.panel.music_view=='queue' and now-self.last_queue>10:
            self.last_queue=now;self.am.refresh_queue()
        track=(self.media.title,self.media.artist)
        if self.media.active and track!=self.queue_track:
            self.queue_track=track
            if not self.panel.dock:self.am.refresh_queue(force=True)
        lenses,viz=self.resolve(self.controls,fade,'')
        if fade<.999 and self.old_controls:
            previous,_=self.resolve(self.old_controls,1-fade,'old:');lenses=previous+lenses
        for lens in lenses:
            lens['rect']=tuple(v/self.ss for v in lens['rect'])
            for key in ('r','strength','bevel'):
                if key in lens:lens[key]/=self.ss
        self.glass.set_lenses(lenses)
        self.glass.set_pointer(None,0,-1,0)
        self.glass.set_panel_shape(morph.x)
        self.glass.set_bubble_audio(0,0,0)
        if viz:
            self.glass.set_viz([v/self.ss for v in viz],base.music_visualizer_bands(self.sound.spectrum,self.sound.reactive_peak),fade)
        else:self.glass.set_viz(None,None,0)
        pw,ph=width.x/self.ss,height.x/self.ss
        force=self.frame_dirty or self.springs.moving or bool(viz) or self.drag is not None
        if self.glass.capture(*self.pos,pw,ph,force,panel_y=self.glass.sp):
            self.glass.render(self.hwnd,*self.pos,pw,ph,fade,(-.55,-.83),self.glassiness,panel_y=self.glass.sp)
            self.frame_dirty=False
        if fade>=.999:self.old_controls=[]

    def set_music_view(self,view):
        self.panel.music_view='now' if view=='bubble' else view
        self.panel.music_menu=None

    def enter_dock(self):
        self.dashboard_pos=list(self.pos)
        work,_=base.work_area_at(*base.cursor_pos())
        self.panel.dock=True;self.panel.dock_open=False;self.panel.page='gaming'
        self._overlay_unlocked=False
        self.panel.update_width()
        self.glass.resize_surface(round(self.panel.DOCK_W*self.S),round(self.panel.DOCK_H*self.S))
        self.pos=[work.left+round(12*self.S)-self.glass.sp,work.top+round(88*self.S)-self.glass.sp]
        self.reset_geometry()
        self.apply_gaming_input()
        self._schedule()

    def reset_geometry(self):
        self.old_controls=[]
        for key,value in (('width',self.panel.w),('height',self.panel.height()),
                          ('dashboard_dock',float(self.panel.dock and not self.panel.dock_open)),('fade',1)):
            sp=self.springs.get(key,value,k=230,zeta=.94);sp.x=sp.target=value;sp.v=0
        self.draw_content()
        self.last_frame=0
        if self.captureable and self.visible:self.refresh_backdrop()

    def refresh_backdrop(self):
        if not self.captureable:return
        was=self.visible
        if was:user32.ShowWindow(self.hwnd,0)
        self.glass.source.frozen=False
        try:
            # Capture the complete buffer, including the folded dock's hidden tail.
            self.glass.capture(*self.pos,self.glass.w,self.glass.hmax,force=True,panel_y=self.glass.sp)
        finally:
            self.glass.source.frozen=True
            if was:user32.ShowWindow(self.hwnd,4)
        self.frame_dirty=True

    def fit_dashboard_to_monitor(self,restore_full=True):
        # Pick the monitor containing this window, not a stale startup rectangle.
        cx=self.pos[0]+self.glass.sp+round(self.panel.window_w*self.S/2)
        cy=self.pos[1]+self.glass.sp+round(min(self.panel.window_h,400)*self.S/2)
        work,_=base.work_area_at(cx,cy)
        full_w=round((work.right-work.left)/self.S-24)
        full_h=round((work.bottom-work.top)/self.S-24)
        self.panel.window_w=full_w if restore_full else min(1100,full_w)
        self.panel.window_h=full_h if restore_full else min(740,full_h)
        self.pos=[work.left+round(12*self.S)-self.glass.sp,
                  work.top+round(12*self.S)-self.glass.sp]

    def leave_dock(self):
        self.panel.dock=False;self.panel.page='music';self._overlay_unlocked=True
        self.pos=self.dashboard_pos or self.pos
        self.fit_dashboard_to_monitor(self.panel.fullscreen)
        self.panel.update_width()
        self.glass.resize_surface(round(self.panel.window_w*self.S),round(self.panel.window_h*self.S))
        self.reset_geometry();self.apply_gaming_input();self._schedule()
        user32.SetForegroundWindow(self.hwnd)

    def click(self,key,x):
        self.panel.focus_key=None
        if key in ('dash:dock','dock:dashboard','dock:expand','dock:collapse','dock:lock',
                   'dash:settings','dash:window','dash:close'):
            if key=='dash:dock':self.enter_dock()
            elif key=='dock:dashboard':self.leave_dock()
            elif key=='dock:lock':self._overlay_unlocked=False;self.apply_gaming_input()
            elif key.startswith('dock:'):
                self.panel.dock_open=key=='dock:expand'
                self.draw_content(crossfade=True)
                if self.captureable:self.refresh_backdrop()
            elif key=='dash:settings':
                self.panel.settings_open=not self.panel.settings_open;self.draw_content(crossfade=True)
            elif key=='dash:close':self.hide();return
            elif key=='dash:window':
                if self.panel.dock:return
                p=self.panel;p.fullscreen=not p.fullscreen
                self.fit_dashboard_to_monitor(p.fullscreen)
                p.update_width();self.glass.resize_surface(round(p.window_w*self.S),round(p.window_h*self.S));self.reset_geometry()
            self.frame_dirty=True;self.frame();return
        super().click(key,x)

    def wndproc(self,hwnd,msg,wp,lp):
        if msg==0x10:  # normal taskbar / Alt+F4 close
            self.quit();return 0
        if msg==0x102 and self.panel.focus_key not in (None,'searchbox'):
            return 0
        # F11 controls the large app only; Escape always gets out of a game dock.
        if msg==base.WM_KEYDOWN and wp==0x7A:
            self.click('dash:window',0);return 0
        if msg==base.WM_KEYDOWN and wp==0x1B and self.panel.dock and self.overlay_unlocked:
            self.leave_dock();return 0
        if msg==base.WM_KEYDOWN and self.visible and self.overlay_unlocked and not self.hotkey_editing:
            if wp==9:
                keys=[k for k in self.panel.rects if k not in ('arthover','mbubble:gesture')]
                if keys:
                    step=-1 if user32.GetAsyncKeyState(0x10)&0x8000 else 1
                    index=keys.index(self.panel.focus_key) if self.panel.focus_key in keys else (-1 if step==1 else 0)
                    self.panel.focus_key=keys[(index+step)%len(keys)]
                    self.draw_content();self.frame_dirty=True
                return 0
            key=self.panel.focus_key
            if key and key.startswith('slider:') and wp in (0x25,0x27,0x24,0x23):
                name=key.split(':',1)[1]
                value=self.sound_value(name)
                value=0 if wp==0x24 else 1 if wp==0x23 else value+(-.02 if wp==0x25 else .02)
                value=max(0,min(1,value));self.set_slider(name,value)
                if name=='seek':
                    if self.media.duration:self.media.seek(value*self.media.duration)
                    self.seek_value=None
                elif name=='glass':
                    cfg=engine.load_config();cfg['glass']=self.glassiness;engine.save_config(cfg)
                elif name!='volume':self.sound.save()
                self.draw_content();self.frame_dirty=True;return 0
            if key and wp in (0x0D,0x20) and not key.startswith('slider:') and key!='searchbox':
                self.click(key,0)
                self.panel.focus_key=key if key in self.panel.rects else None
                self.draw_content();self.frame_dirty=True;return 0
        return super().wndproc(hwnd,msg,wp,lp)


def dashboard_startup(enable):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,base.RUN_KEY) as key:
        if enable:
            pythonw=Path(sys.executable).with_name('pythonw.exe')
            winreg.SetValueEx(key,'Blob Dashboard',0,winreg.REG_SZ,
                f'"{pythonw}" "{Path(__file__).with_name("app.pyw")}" --startup')
        else:
            try:winreg.DeleteValue(key,'Blob Dashboard')
            except FileNotFoundError:pass


def configure_runtime():
    """Isolate process-local globals before constructing any controller."""
    engine.DATA_DIR=os.path.join(os.environ['LOCALAPPDATA'],'Blob-Dashboard')
    engine.CONFIG_PATH=os.path.join(engine.DATA_DIR,'config.json')
    engine.LOG_PATH=os.path.join(engine.DATA_DIR,'blob.log')
    os.makedirs(engine.DATA_DIR,exist_ok=True)
    cfg=engine.load_config()
    if not cfg:
        engine.save_config({'glass':.35,'pointer':'system','captureable':False,
                            'options':{'musicreactive':True,'soundstart':False},
                            'overlay_hotkey':{'mods':3,'vk':68}})
    base.APP_NAME='Blob Dashboard'
    base.set_startup=dashboard_startup
    base.Panel=DashboardPanel
    glass.GlassRenderer=DashboardRenderer
    os.environ['BLOB_PAGE']='music'
    os.environ['BLOB_COMPACT']='0'
    os.environ['BLOB_MVIEW']='now'
    dpi=user32.GetDpiForSystem();scale=dpi/96
    work,_=base.work_area_at(*base.cursor_pos())
    DashboardPanel.WIDE=DashboardPanel.GAMING_WIDE=round((work.right-work.left)/scale-24)
    DashboardPanel.DASH_H=round((work.bottom-work.top)/scale-24)


def main():
    try:ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:pass
    base.k32.CreateMutexW.restype=base.wintypes.HANDLE
    mutex=base.k32.CreateMutexW(None,False,'Local\\BlobDashboard')
    if base.k32.GetLastError()==183:return
    configure_runtime()
    try:DashboardApp().run()
    except Exception:
        import traceback
        engine.log('fatal: '+traceback.format_exc())
        raise


if __name__=='__main__':main()
