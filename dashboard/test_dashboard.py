"""No live controller / routing writes. Native layout and action regressions."""
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
import test_regressions as reg
import dashboard as dash


class DashboardTests(unittest.TestCase):
    def setUp(self):self.snap,self.sound,self.media,self.am=reg.fixtures()

    def panel(self,w=1120,h=740,scale=1):
        p=dash.DashboardPanel(scale)
        p.window_w=w;p.window_h=h;p.am=self.am
        p.game=dict(fps=144,frame_ms=6.94,ram_percent=40,ram_gb=12)
        return p

    def draw(self,p):p.draw(self.snap,self.sound,.35,False,media=self.media)

    def test_overview_all_panels_and_dpi(self):
        for w,h in ((900,620),(1120,740),(1680,980)):
            for scale in (1,1.5,2):
                p=self.panel(w,h,scale);self.draw(p)
                self.assertTrue({'music','system','sound','gaming','album'}<=p.regions.keys())
                reg.LayoutTests.assert_bounds(self,p)
                a=p.regions['album'];self.assertEqual(a[2]-a[0],a[3]-a[1])
                for group in ('gaming','music','system','sound'):
                    box=p.regions[group]
                    for other in ('gaming','music','system','sound'):
                        if group!=other:self.assertFalse(reg.intersects(box,p.regions[other]))

    def test_settings_and_lists(self):
        for view in ('now','art','search','queue'):
            p=self.panel();p.music_view=view;p.settings_open=True
            self.draw(p);reg.LayoutTests.assert_bounds(self,p)
            self.assertIn('toggle:musicreactive',p.rects)
            self.assertNotIn('size:compact',p.rects)

    def test_dock_dimensions_and_unknown_data(self):
        for opened in (False,True):
            p=self.panel(scale=1.5);p.dock=True;p.dock_open=opened;p.game={}
            self.draw(p);reg.LayoutTests.assert_bounds(self,p)
            self.assertEqual(p.w,round(92*p.S))
            if not opened:self.assertIn('dock:expand',p.rects)
            else:self.assertIn('dock:collapse',p.rects)
            self.assertTrue(any('—' in text for text,_ in p.labels))

    def test_expansion_keeps_top_left_fixed(self):
        a=dash.DashboardApp.__new__(dash.DashboardApp)
        a.panel=self.panel();a.panel.dock=True
        a.pos=[-16,60];a.glass=NS(sp=28)
        a.draw_content=Mock();a.frame=Mock();a.captureable=False
        before=a.pos.copy()
        a.click('dock:expand',0)
        self.assertTrue(a.panel.dock_open)
        self.assertEqual(a.pos,before)
        self.assertEqual(a._panel_y(90),a._panel_y(480))
        a.click('dock:collapse',0)
        self.assertFalse(a.panel.dock_open)
        self.assertEqual(a.pos,before)

    def test_navigation_is_local_not_smallblob_tabs(self):
        a=dash.DashboardApp.__new__(dash.DashboardApp)
        a.panel=self.panel()
        a.set_music_view('queue')
        self.assertEqual(a.panel.page,'music')
        self.assertEqual(a.panel.music_view,'queue')
        a.panel.update_width()
        self.assertEqual(a.panel.w,1120*a.panel.S)

    def test_top_left_renderer_anchor(self):
        r=dash.DashboardRenderer.__new__(dash.DashboardRenderer);r.sp=28
        self.assertEqual(r.panel_x(92),28)
        self.assertEqual(r.panel_x(1120),28)
        self.assertEqual(r.panel_y(87),28)
        self.assertEqual(r.panel_y(480),28)

    def test_startup_targets_dashboard_not_smallblob(self):
        with patch.object(dash.winreg,'CreateKey') as key, patch.object(dash.winreg,'SetValueEx') as write:
            dash.dashboard_startup(True)
        command=write.call_args.args[-1]
        self.assertIn('dashboard',command)
        self.assertIn('app.pyw',command)
        self.assertNotIn('blob.pyw',command)

    def test_overview_can_use_the_global_lock(self):
        a=dash.DashboardApp.__new__(dash.DashboardApp);a.panel=self.panel()
        with patch.object(dash.base.App,'toggle_overlay_input') as toggle:
            self.assertTrue(a.overlay_lock_available)
            a.toggle_overlay_input();toggle.assert_called_once()
            a.panel.dock=True
            self.assertTrue(a.overlay_lock_available)
            a.toggle_overlay_input();self.assertEqual(toggle.call_count,2)

    def test_restore_uses_current_work_area(self):
        a=dash.DashboardApp.__new__(dash.DashboardApp);a.panel=self.panel()
        a.S=1;a.pos=[2100,200];a.glass=NS(sp=28)
        work=NS(left=1920,top=0,right=3840,bottom=1040)
        with patch.object(dash.base,'work_area_at',return_value=(work,None)):
            a.fit_dashboard_to_monitor()
        self.assertEqual(a.pos,[1904,-16])
        self.assertEqual((a.panel.window_w,a.panel.window_h),(1896,1016))

    def test_resize_recaptures_when_screenshot_mode_is_on(self):
        a=dash.DashboardApp.__new__(dash.DashboardApp);a.panel=self.panel()
        a.springs=dash.base.Springs();a.captureable=a.visible=True
        a.draw_content=Mock();a.refresh_backdrop=Mock()
        a.reset_geometry();a.refresh_backdrop.assert_called_once()

    def test_keyboard_focus_activation_and_slider(self):
        a=dash.DashboardApp.__new__(dash.DashboardApp);a.panel=self.panel();self.draw(a.panel)
        a.visible=a._overlay_unlocked=True;a.hotkey_editing=False
        a.draw_content=Mock();a.click=Mock();a.sound_value=Mock(return_value=.5)
        a.set_slider=Mock();a.sound=Mock()
        with patch.object(dash.user32,'GetAsyncKeyState',return_value=0):
            a.wndproc(1,dash.base.WM_KEYDOWN,9,0)
        self.assertIsNotNone(a.panel.focus_key)
        key=a.panel.focus_key
        a.wndproc(1,dash.base.WM_KEYDOWN,0x0D,0)
        a.click.assert_called_once_with(key,0)
        a.panel.focus_key='slider:boost'
        a.wndproc(1,dash.base.WM_KEYDOWN,0x27,0)
        a.set_slider.assert_called_once_with('boost',.52)
        a.sound.save.assert_called_once()

    def test_frozen_expansion_refreshes_full_dock_height(self):
        a=dash.DashboardApp.__new__(dash.DashboardApp);a.panel=self.panel()
        a.panel.dock=True;a.pos=[0,0];a.captureable=True;a.visible=True;a.hwnd=1
        a.draw_content=Mock();a.frame=Mock()
        a.glass=NS(w=92,hmax=480,sp=28,source=NS(frozen=True),capture=Mock())
        with patch.object(dash.user32,'ShowWindow'):
            a.click('dock:expand',0)
        a.glass.capture.assert_called_once_with(0,0,92,480,force=True,panel_y=28)
        self.assertTrue(a.glass.source.frozen)

    def test_temperature_warning_and_keyboard_focus_are_rendered(self):
        import numpy as np
        p=self.panel();self.draw(p)
        self.assertGreater(np.asarray(p.accent)[...,3].sum(),0)
        p.settings_open=True;self.draw(p)
        before=np.asarray(p.ink).copy()
        p.focus_key='slider:glass';p.draw_focus()
        self.assertGreater(np.abs(np.asarray(p.ink).astype(float)-before).sum(),100)


if __name__=='__main__':unittest.main()
