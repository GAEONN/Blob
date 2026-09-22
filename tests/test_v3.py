import math
import struct
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import test_regressions as reg
from reactive import AudioMotion

blob, engine = reg.blob, reg.blob.engine


class MotionTests(unittest.TestCase):
    def test_visualizer_works_without_boost_and_does_not_invent_bands(self):
        import numpy as np
        self.assertTrue(np.all(blob.music_visualizer_bands(np.zeros(28), .25) == .5))
        self.assertTrue(np.all(blob.music_visualizer_bands(np.zeros(28), 0) == 0))
        spectrum = np.linspace(0, 1, 28, dtype=np.float32)
        np.testing.assert_array_equal(blob.music_visualizer_bands(spectrum, .1), spectrum)

    def test_loud_and_quiet_audio_keep_dynamics(self):
        for gain in (.03, .9):
            motion = AudioMotion()
            samples = []
            for i in range(600):
                level = gain * (.65 + .35*math.sin(i/5))
                samples.append(motion.update((level, level, level), 1/60)[0])
            self.assertGreater(max(samples[120:])-min(samples[120:]), .12)
            self.assertLess(max(samples), .95)

    def test_constant_loud_signal_settles_and_silence_stops(self):
        motion = AudioMotion()
        for _ in range(300):
            result = motion.update((1, 1, 1), 1/60)
        self.assertLess(max(result), .05)
        for _ in range(120):
            result = motion.update((0, 0, 0), 1/60)
        self.assertLess(max(result), .001)

    def test_invalid_audio_is_bounded(self):
        result = AudioMotion().update((float('nan'), -1, float('inf')), .016)
        self.assertEqual(result, (0, 0, 0))


class SensorTests(unittest.TestCase):
    def test_partial_provider_does_not_mask_fans_or_gpu(self):
        result = engine.merge_sensor_feeds(
            ('hwmonitor', {'cpu': 60, 'fans': []}),
            ('hwinfo', {'gpu': 52, 'fans': [0, 1700], 'fan_names': ['GPU fan', 'CPU fan']}))
        self.assertEqual(result['fans'], [0, 1700])
        self.assertEqual(result['cpu'], 60)
        self.assertEqual(result['gpu'], 52)
        self.assertEqual(result['cpu_source'], 'hwmonitor')

    def test_fans_keep_names_zero_and_reject_invalid_values(self):
        result = engine.merge_sensor_feeds(
            ('lhm', {'fans': [1000, 0, float('nan'), -1], 'fan_names': ['CPU fan', 'GPU fan', 'Bad', 'Bad2']}),
            ('hwinfo', {'fans': [1010, 950], 'fan_names': ['CPU Fan', 'Case fan']}))
        self.assertEqual(result['fans'], [1000, 0, 1010, 950])
        self.assertEqual(result['fan_names'], ['CPU fan', 'GPU fan', 'CPU Fan', 'Case fan'])
        self.assertEqual(result['fan_sources'], ['lhm', 'lhm', 'hwinfo', 'hwinfo'])

    def test_identically_named_distinct_fans_are_preserved(self):
        feed = {'fans': [1300, 1400], 'fan_names': ['Fan 1', 'Fan 1'],
                'fan_ids': ['/cpu/fan/1', '/gpu/fan/1']}
        result = engine.merge_sensor_feeds(('lhm', feed), ('lhm', feed))
        self.assertEqual(result['fans'], [1300, 1400])
        self.assertEqual(result['fan_ids'], feed['fan_ids'])

    def hwinfo_data(self, age=0):
        # SDK's #pragma pack(1): 44-byte base header and value at byte 284.
        data = bytearray(48+316*2)
        struct.pack_into('<4sIIqIIIIII', data, 0, b'SiWH', 1, 1, int(time.time())-age, 48, 264, 0, 48, 316, 2)
        for i, value in enumerate((0, 1875.0)):
            base = 48+316*i
            struct.pack_into('<III', data, base, 3, 0, i)
            name = f'Fan {i+1}'.encode()
            data[base+140:base+140+len(name)] = name
            struct.pack_into('<d', data, base+284, value)
        return bytes(data)

    def test_hwinfo_packed_struct_and_stopped_fan(self):
        data = self.hwinfo_data()
        class Mapping:
            def __enter__(self): return data
            def __exit__(self, *args): pass
        with patch('mmap.mmap', return_value=Mapping()):
            result = engine.HWiNFOShared().poll()
        self.assertEqual(result['fans'], [0, 1875])
        self.assertEqual(result['fan_names'], ['Fan 1', 'Fan 2'])

    def test_hwinfo_stale_mapping_is_not_live_telemetry(self):
        data = self.hwinfo_data(age=60)
        class Mapping:
            def __enter__(self): return data
            def __exit__(self, *args): pass
        with patch('mmap.mmap', return_value=Mapping()):
            self.assertEqual(engine.HWiNFOShared().poll(), {})

    def test_oem_fallback_never_opens_non_asus_device(self):
        with patch.object(engine, 'reg_str', return_value='Dell Inc.'), patch.object(engine, 'open_device') as opened:
            self.assertEqual(engine.AsusReadOnly().poll()['fans'], [])
            opened.assert_not_called()


class V3LayoutTests(unittest.TestCase):
    setUp = reg.LayoutTests.setUp
    panel = reg.LayoutTests.panel
    draw = reg.LayoutTests.draw
    assert_bounds = reg.LayoutTests.assert_bounds
    def test_visualizer_button_hit_toggle_and_cover_region(self):
        from unittest.mock import Mock
        for compact in (False, True):
            p = self.panel(compact=compact, page='music', view='art')
            p.music_menu = 'options'
            p.options['musicreactive'] = True
            self.draw(p)
            box = p.rects['toggle:musicreactive']
            key = p.hit((box[0]+box[2])/2, (box[1]+box[3])/2)
            self.assertEqual(key, 'toggle:musicreactive')
            self.assertTrue(any(c[0] == 'viz' for c in p.controls))
            a = blob.App.__new__(blob.App)
            a.panel, a.draw_content, a.frame = p, Mock(), Mock()
            with patch.object(engine, 'load_config', return_value={}), patch.object(engine, 'save_config'):
                a.click(key, 0)
                self.draw(p)
                self.assertFalse(any(c[0] == 'viz' for c in p.controls))
                a.click(key, 0)
                self.draw(p)
                self.assertTrue(any(c[0] == 'viz' for c in p.controls))

    def test_gaming_bubble_only_displays_fps_and_frame_time(self):
        for scale in (1, 1.25, 1.5, 2):
            p = self.panel(scale=scale, page='gaming')
            p.gaming_view = 'bubble'
            p.game = {'fps': 144, 'frame_ms': 6.94}
            self.draw(p)
            self.assert_bounds(p)
            self.assertEqual([t for t, _ in p.labels], ['144', 'FPS', '6.9 ms'])
            self.assertIn('gcycle', p.rects)
            self.assertNotIn('gview:restore', p.rects)
            p.tool_reveal = 1.0
            self.draw(p)
            self.assertIn('gview:restore', p.rects)

    def test_settings_all_text_boxes_have_vertical_clearance(self):
        for scale in (1, 1.5, 2):
            for compact in (False, True):
                p = self.panel(scale=scale, compact=compact, page='settings')
                for row in range(32):
                    p.scroll['settings'] = row
                    self.draw(p)
                    labels = [(t, b) for t, b in p.labels if b[3] < p.height(None)-70*p.S]
                    for i, (text, box) in enumerate(labels):
                        for other, bounds in labels[i+1:]:
                            self.assertFalse(reg.intersects(box, bounds), (compact,row,text,other))

    def test_cover_is_square_with_equal_insets_and_visible_back(self):
        for compact in (False, True):
            p = self.panel(compact=compact, page='music', view='art')
            p.hover_key = None
            self.draw(p)
            self.assertEqual(p.w, p.height(None))
            x0,y0,x1,y1 = p.rects['arthover']
            self.assertEqual((x0,y0,p.w-x1,p.height(None)-y1), (4*p.S,)*4)
            self.assertIn('mview:now', p.rects)

    def test_compact_album_is_square_not_round(self):
        p = self.panel(compact=True, page='music')
        self.draw(p)
        x0,y0,x1,y1 = map(int,p.rects['mview:art'])
        self.assertEqual(x1-x0,y1-y0)
        self.assertEqual(p.pic.getpixel((x0+int(7*p.S), y0+int(p.S)))[3],255)

    def test_hardware_body_and_satellite_are_direct_drag_handles(self):
        a = blob.App.__new__(blob.App)
        a.panel = NS(page='blob', hardware_view='bubble')
        a.visible, a._overlay_unlocked, a.drag_click = True, True, None
        a.hover = 'hcycle'
        self.assertTrue(a.bubble_drag_active)
        self.assertTrue(a.bubble_drag_key('hcycle'))
        a.hover = 'hview:card'
        self.assertTrue(a.bubble_drag_active)
        self.assertTrue(a.bubble_drag_key('hview:card'))
        self.assertFalse(a.bubble_drag_key('tool:calculator'))


if __name__ == '__main__':
    unittest.main()
