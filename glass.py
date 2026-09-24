"""Liquid-glass renderer for a Win32 layered window.

The renderer keeps a current copy of the pixels behind the panel (the window is excluded from
capture, so we never see ourselves), refreshes that copy independently, and uploads it to the
GPU when it changes. One fragment shader evaluates every glass shape analytically and does the
refraction, dispersion, frosted blur, lighting and per-pixel adaptive text colour; the result
is read back and pushed with UpdateLayeredWindow (per-pixel alpha). Shapes can animate every
frame without recapturing the desktop each time.

Shapes are true squircles: the figma-squircle construction (corner radius + corner smoothing,
60 % = Apple's iOS value) — Bézier ramps into a shortened circular arc, so curvature is
continuous. The panel AND every control inside it (mode track, selected thumb, hover pills)
are glass lenses: each one bends the background along its own squircle outline.
"""
import ctypes
import math
import time
from ctypes import wintypes
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")

HDC = HBITMAP = HGDIOBJ = ctypes.c_void_p
user32.GetDC.restype = HDC
user32.GetDC.argtypes = [wintypes.HWND]
gdi32.CreateCompatibleDC.restype = HDC
gdi32.CreateCompatibleDC.argtypes = [HDC]
gdi32.CreateDIBSection.restype = HBITMAP
gdi32.CreateDIBSection.argtypes = [HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteDC.argtypes = [HDC]
gdi32.BitBlt.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, HDC, ctypes.c_int,
                         ctypes.c_int, wintypes.DWORD]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(SIZE),
                                       HDC, ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
                                       ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]


class Dib:
    """Top-down 32-bit BGRA DIB section exposed as a numpy array."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.dc = gdi32.CreateCompatibleDC(None)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        self.bmp = gdi32.CreateDIBSection(self.dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        self.old = gdi32.SelectObject(self.dc, self.bmp)
        buf = (ctypes.c_ubyte * (w * h * 4)).from_address(bits.value)
        self.arr = np.ctypeslib.as_array(buf).reshape(h, w, 4)

    def free(self):
        gdi32.SelectObject(self.dc, self.old)
        gdi32.DeleteObject(self.bmp)
        gdi32.DeleteDC(self.dc)


# ─────────────────────────── squircle geometry (figma-squircle) ───────────────────────────
def _bezier(p0, p1, p2, p3, n):
    t = np.linspace(0, 1, n)[:, None]
    return ((1 - t) ** 3) * p0 + 3 * ((1 - t) ** 2) * t * p1 + 3 * (1 - t) * t * t * p2 + t ** 3 * p3


def squircle_polygon(w, h, r, smoothing=0.6):
    """Outline of a w×h squircle, clockwise from the top edge. Same maths as figma-squircle."""
    budget = min(w, h) / 2
    r = min(r, budget)
    if r <= 0:
        return [(0, 0), (w, 0), (w, h), (0, h)]
    s = min(smoothing, max(0.0, budget / r - 1))
    p = min((1 + s) * r, budget)
    arc = 90 * (1 - s)
    arc_len = math.sin(math.radians(arc / 2)) * r * math.sqrt(2)
    alpha = (90 - arc) / 2
    p34 = r * math.tan(math.radians(alpha / 2))
    beta = 45 * s
    c = p34 * math.cos(math.radians(beta))
    d = c * math.tan(math.radians(beta))
    b = (p - arc_len - c - d) / 3
    a = 2 * b

    # top-right corner, in coordinates relative to that corner (x: from right edge, y: from top)
    P0 = np.array([-p, 0.0])
    b1 = _bezier(P0, P0 + (a, 0), P0 + (a + b, 0), P0 + (a + b + c, d), 14)
    E1 = P0 + (a + b + c, d)
    E2 = E1 + (arc_len, arc_len)
    u = a + b + c - p
    k = (-(u - d) + math.sqrt(max(0.0, (u - d) ** 2 - 2 * (u * u + d * d - r * r)))) / 2
    cx, cy = -k, k
    t1, t2 = math.atan2(E1[1] - cy, E1[0] - cx), math.atan2(E2[1] - cy, E2[0] - cx)
    ang = np.linspace(t1, t2, 14)
    arc_pts = np.stack([cx + r * np.cos(ang), cy + r * np.sin(ang)], 1)
    b2 = _bezier(E2, E2 + (d, c), E2 + (d, b + c), E2 + (d, a + b + c), 14)
    corner = np.concatenate([b1, arc_pts[1:], b2[1:]])  # from (-p,0) to (0,p)

    tr = np.stack([w + corner[:, 0], corner[:, 1]], 1)
    br = np.stack([w + corner[::-1, 0], h - corner[::-1, 1]], 1)
    bl = np.stack([-corner[:, 0], h - corner[:, 1]], 1)
    tl = np.stack([-corner[::-1, 0], corner[::-1, 1]], 1)
    return [tuple(pt) for pt in np.concatenate([tr, br, bl, tl])]


_field_cache = {}


def squircle_field(w, h, r, smoothing=0.6):
    """(coverage 0..1, signed distance px [negative inside], outward normals nx, ny)."""
    key = (int(w), int(h), round(r, 2), smoothing)
    if key in _field_cache:
        return _field_cache[key]
    w, h = max(2, int(w)), max(2, int(h))
    k = 4
    hi = Image.new("L", (w * k, h * k), 0)
    ImageDraw.Draw(hi).polygon([(x * k, y * k) for x, y in squircle_polygon(w, h, r, smoothing)], fill=255)
    cov = np.asarray(hi.resize((w, h), Image.BOX), np.float32) / 255
    inside = np.pad(np.asarray(hi) > 127, k)  # pad so the frame edge counts as "outside"
    d_in = ndimage.distance_transform_edt(inside)[k:-k, k:-k]
    d_out = ndimage.distance_transform_edt(~inside)[k:-k, k:-k]
    sd_hi = (d_out - d_in) / k
    sd = sd_hi[k // 2::k, k // 2::k][:h, :w].astype(np.float32)
    smooth = ndimage.gaussian_filter(sd, 1.2)
    gy, gx = np.gradient(smooth)
    n = np.sqrt(gx * gx + gy * gy) + 1e-6
    res = (cov, sd, (gx / n).astype(np.float32), (gy / n).astype(np.float32))
    if len(_field_cache) > 64:
        _field_cache.clear()
    _field_cache[key] = res
    return res


@lru_cache(maxsize=64)
def shape_mask(w, h, r, smoothing=0.6):
    # Artwork needs coverage only. Computing two 4x distance transforms and normals
    # here used hundreds of MB and stalled the UI for an otherwise simple mask.
    w, h = max(2, int(w)), max(2, int(h))
    k = 4
    hi = Image.new("L", (w * k, h * k), 0)
    ImageDraw.Draw(hi).polygon([(x * k, y * k) for x, y in squircle_polygon(w, h, r, smoothing)], fill=255)
    cov = np.asarray(hi.resize((w, h), Image.BOX), np.float32) / 255
    cov.setflags(write=False)
    return cov


# ─────────────────────────── GPU glass renderer ───────────────────────────
# All shapes are evaluated analytically per pixel on the GPU (superellipse corners, n≈5, the
# squircle family; n=2 gives exact capsules/circles), so every piece of glass can move, stretch
# and morph every frame at no CPU cost.
MAX_LENSES = 96

BLUR_FRAG = """
#version 330
// One direction of a separable Gaussian at quarter resolution. Real frosted
// glass is a smooth blur; scattered taps from a coarse mip read as blotches.
uniform sampler2D src;
uniform vec2 step_uv;      // one output texel along the blur direction
uniform float src_lod;     // 2.0 reads the capture's quarter-size mip level
uniform vec2 out_size;
uniform float sigma;       // in output texels
out vec4 frag;
void main() {
    vec2 uv = gl_FragCoord.xy / out_size;
    vec4 acc = vec4(0.0);
    float total = 0.0;
    for (int i = -10; i <= 10; i++) {
        float w = exp(-float(i * i) / (2.0 * sigma * sigma));
        acc += w * textureLod(src, uv + step_uv * float(i), src_lod);
        total += w;
    }
    frag = acc / total;
}
"""

VERT = """
#version 330
in vec2 pos;
void main() { gl_Position = vec4(pos, 0.0, 1.0); }
"""

FRAG = """
#version 330
uniform sampler2D bg;                 // screen behind the panel (BGRA bytes), mipmapped
uniform sampler2D ink0, ink1;         // text coverage: previous / current content
uniform sampler2D acc0, acc1;         // accent colours (premultiplied): previous / current
uniform sampler2D pic0, pic1;         // images (album art), premultiplied, drawn as-is
uniform sampler2D backdrop0, backdrop1; // moving blurred album backdrop, premultiplied
uniform vec4 ambient;                 // page tint at the top (e.g. the album's colour), alpha = strength
uniform vec3 ambient2;                // page tint at the bottom
uniform vec2 ink0_size, ink1_size;   // in texels (content is rendered supersampled)
uniform float ink_ss;                // texels per window pixel
uniform float fade;                   // 0 = previous content, 1 = current
uniform vec2 bg_size, cap_origin;     // window px + cap_origin = bg px
uniform vec2 panel_pos, panel_size;   // panel rect in window px
uniform float panel_r;
uniform float panel_morph;            // 0 card, 1 contextual bubble; continuous during transition
uniform vec3 bubble_audio;             // smoothed bass, mid and treble energy, each 0..1
uniform float bubble_side;             // +1 right edge, -1 left edge
uniform float bubble_time;             // monotonic seconds for the audio wave
uniform float bubble_proximity;         // 0 idle, 1 cursor is inside the bubble
uniform float tool_expansion;           // expanded satellite canvas; keeps the base bubble fixed
uniform float tool_card;                // top-anchored calculator with strict panel clipping
uniform vec4 magnifier_rect;            // clear reading aperture in panel pixels
uniform sampler2D bgblur;               // Gaussian-blurred capture, quarter size
uniform sampler2D busy_map;             // 0 calm wallpaper .. 1 text-heavy windows, per area of the panel
uniform float magnifier_zoom;           // zero disables; positive values magnify live desktop
uniform vec4 backdrop_motion;          // scale, x drift, y drift, rotation
uniform float backdrop_time;
uniform float backdrop_alpha;
uniform vec4 backdrop_color0;
uniform vec4 backdrop_color1;
uniform vec4 backdrop_color2;
uniform vec4 backdrop_color3;
uniform vec4 backdrop_color4;
uniform vec4 backdrop_color5;
uniform vec2 light;
uniform float S, glassiness;
uniform int n_lens;
uniform vec4 L_rect[96];  // x0 y0 x1 y1 (panel px)
uniform vec4 L_a[96];     // radius, strength, bevel, zoom
uniform vec4 L_b[96];     // rim gain, frost, lift, raised
uniform vec4 L_c[96];     // tint rgb, tint alpha
uniform vec4 L_d[96];     // ink fill, corner exponent, -, -
uniform vec4 viz_rect;
uniform float bands[28];
uniform float viz_alpha;
uniform vec2 ptr_pos;       // pointer tip, panel px
uniform float ptr_size;     // pointer height in px
uniform float ptr_alpha;    // free-floating pointer visibility
uniform int ptr_target;     // lens the pointer is fused with (-1: none)
uniform float ptr_k;        // surface-tension radius of the fusion (smooth-min)
out vec4 frag;

const vec2 TAPS[12] = vec2[](vec2(-0.326,-0.406), vec2(-0.840,-0.074), vec2(-0.696, 0.457),
    vec2(-0.203, 0.621), vec2( 0.962,-0.195), vec2( 0.473,-0.480), vec2( 0.519, 0.767),
    vec2( 0.185,-0.893), vec2( 0.507, 0.064), vec2( 0.896, 0.412), vec2(-0.322,-0.933),
    vec2(-0.792,-0.598));

vec3 at(vec2 px, float lod) { return textureLod(bg, px / bg_size, lod).bgr; }
float luma(vec3 c) { return dot(c, vec3(0.299, 0.587, 0.114)); }
// Luma of the glass over scenery of luma r: the same dark/light lean applied to the glass below.
float glassOver(float r, float push) {
    float d = smoothstep(0.50, 0.66, r);
    return mix(mix(r, 0.0, push * (1.0 - d)), 1.0, (push + 0.08) * d);
}

const float SQUIRCLE_N = 2.2;      // fitted against figma-squircle, 60 % corner smoothing
const float SQUIRCLE_K = 1.08;     // radius factor that goes with it

float sdShape(vec2 p, vec2 c, vec2 hs, float r, float n) {
    vec2 q = abs(p - c) - hs + vec2(r);
    vec2 qp = max(q, vec2(0.0)) / max(r, 1e-3);
    float corner = r * pow(pow(qp.x, n) + pow(qp.y, n) + 1e-12, 1.0 / n);
    return corner + min(max(q.x, q.y), 0.0) - r;
}

float smin(float a, float b, float k);
float lensSd(int i, vec2 p);

float sdMagnifierRod(vec2 p) {
    vec2 centre = (magnifier_rect.xy + magnifier_rect.zw) * 0.5;
    vec2 a = centre + vec2(75.0 * S), b = centre + vec2(164.0 * S);
    vec2 v = b - a;
    return length(p - a - v * clamp(dot(p-a, v) / dot(v, v), 0.0, 1.0)) - 18.0*S;
}

float sdPanel(vec2 p) {
    if (magnifier_zoom > 0.0) {
        vec2 centre = (magnifier_rect.xy + magnifier_rect.zw) * 0.5;
        float radius = (magnifier_rect.z - magnifier_rect.x) * 0.5 + 8.0*S;
        return smin(length(p-centre)-radius, sdMagnifierRod(p), 9.0*S);
    }
    vec2 c = panel_size * 0.5;
    float card = sdShape(p, c, c, panel_r * SQUIRCLE_K, SQUIRCLE_N);
    // A smoothly fused 78 px mode body and 43 px restore satellite. The ratios
    // match Panel.BUBBLE_W/H. Relative centres let the two lobes emerge while
    // the card is contracting instead of appearing only after the resize.
    // The tool satellites use a larger transparent canvas, but the original
    // Blob bubble stays a fixed 104x98 DIP shape at the monitor-facing edge.
    float fixed_u = max(S, 0.001);
    float expanded = clamp(tool_expansion, 0.0, 1.0);
    float u = mix(min(panel_size.x / 104.0, panel_size.y / 98.0), fixed_u, expanded);
    float base_w = 104.0 * fixed_u;
    float origin_x = expanded > 0.001 && bubble_side > 0.0 ? panel_size.x - base_w : 0.0;
    float bass = bubble_audio.x, mid = bubble_audio.y, treble = bubble_audio.z;
    float main_x = bubble_side < 0.0 ? 61.0 : 43.0;
    float nub_x = bubble_side < 0.0 ? 21.5 : 82.5;
    vec2 main_c = vec2(origin_x + main_x * u, 55.0 * u) +
                  vec2(0.0, -4.0 * bass * u);
    vec2 nub_c = vec2(origin_x + nub_x * u, 22.5 * u) +
                 vec2(bubble_side * (-2.2 * mid - 1.8 * treble) * u,
                      (1.5 * mid + 1.8 * treble) * u);
    // Bass makes the body inhale vertically; mids pull the lobes together and
    // thicken their liquid neck; treble gives the satellite a quick response.
    vec2 main_q = p - main_c;
    main_q /= vec2(1.0 + .030 * mid, 1.0 + .100 * bass);
    vec2 nub_q = p - nub_c;
    nub_q /= vec2(1.0 + .035 * treble, 1.0 + .055 * treble);
    float energy = clamp(.75 * bass + .45 * mid + .25 * treble, 0.0, 1.0);
    float angle = atan(main_q.y, main_q.x);
    float wave = (0.90 * sin(angle * 4.0 - bubble_time * 5.0) +
                  0.42 * sin(angle * 7.0 + bubble_time * 3.1)) * energy * u;
    float a = length(main_q) - (39.0 + 2.0 * bass + 1.0 * mid) * u - wave;
    float b = length(nub_q) - (21.5 + 1.4 * treble) * u;
    float k = (9.0 + 5.0 * mid + 1.0 * bass) * u;
    float h = max(k - abs(a - b), 0.0) / max(k, 1e-3);
    float bubble = min(a, b) - h * h * k * 0.25;

    // The bubble has two intentional cursor-distance stages. At rest it is a
    // quiet circle; approaching it pulls the familiar asymmetric satellite
    // back into view. This remains an SDF operation, so the edge stays
    // refractive without a CPU redraw.
    float proximity = clamp(bubble_proximity, 0.0, 1.0);
    float approach = smoothstep(0.0, 0.62, proximity);
    float idle = length(main_q) - (39.0 + 1.0 * bass) * u;
    float approaching = mix(idle, bubble, approach);
    float t = smoothstep(0.0, 1.0, clamp(panel_morph, 0.0, 1.0));
    float shape = mix(card, approaching, t);
    if (expanded > 0.0 && t > .8) {
        // Fold every lobe into one continuous smooth union. Blending each
        // lobe independently with the body and then taking min() leaves cusp
        // seams where two blends meet; those were the visible palette spikes.
        float palette = shape;
        for (int i = 0; i < n_lens; i++) {
            if (L_d[i].y < 0.0)
                palette = smin(palette, lensSd(i, p), 18.0*S);
        }
        shape = palette;
    }
    return shape;
}

vec2 nPanel(vec2 p) {
    vec2 g = vec2(sdPanel(p + vec2(0.5, 0.0)) - sdPanel(p - vec2(0.5, 0.0)),
                  sdPanel(p + vec2(0.0, 0.5)) - sdPanel(p - vec2(0.0, 0.5)));
    return g / max(length(g), 1e-5);
}
vec2 nShape(vec2 p, vec2 c, vec2 hs, float r, float n) {
    vec2 g = vec2(sdShape(p + vec2(0.5, 0.0), c, hs, r, n) - sdShape(p - vec2(0.5, 0.0), c, hs, r, n),
                  sdShape(p + vec2(0.0, 0.5), c, hs, r, n) - sdShape(p - vec2(0.0, 0.5), c, hs, r, n));
    return g / max(length(g), 1e-5);
}
// pointer shapes (unit = pointer height, tip near the origin = hotspot)
uniform int ptr_shape;   // 0 triangle, 1 arrow (rounded dart with a notch), 2 droplet
const vec2 AV[3] = vec2[](vec2(0.11, 0.11), vec2(0.86, 0.45), vec2(0.43, 0.88));
const vec2 PV[4] = vec2[](vec2(0.12, 0.12), vec2(1.00, 0.54), vec2(0.55, 0.63), vec2(0.24, 0.96));
float sdPoly3(vec2 q) {
    float d = dot(q - AV[0], q - AV[0]), s = 1.0;
    for (int i = 0, j = 2; i < 3; j = i, i++) {
        vec2 e = AV[j] - AV[i], w = q - AV[i];
        vec2 b = w - e * clamp(dot(w, e) / dot(e, e), 0.0, 1.0);
        d = min(d, dot(b, b));
        bvec3 c = bvec3(q.y >= AV[i].y, q.y < AV[j].y, e.x * w.y > e.y * w.x);
        if (all(c) || all(not(c))) s = -s;
    }
    return s * sqrt(d);
}
float sdPoly4(vec2 q) {
    float d = dot(q - PV[0], q - PV[0]), s = 1.0;
    for (int i = 0, j = 3; i < 4; j = i, i++) {
        vec2 e = PV[j] - PV[i], w = q - PV[i];
        vec2 b = w - e * clamp(dot(w, e) / dot(e, e), 0.0, 1.0);
        d = min(d, dot(b, b));
        bvec3 c = bvec3(q.y >= PV[i].y, q.y < PV[j].y, e.x * w.y > e.y * w.x);
        if (all(c) || all(not(c))) s = -s;
    }
    return s * sqrt(d);
}
float sdPointer(vec2 p) {
    vec2 q = (p - ptr_pos) / ptr_size;
    float d;
    if (ptr_shape == 0) d = sdPoly3(q) - 0.10;
    else if (ptr_shape == 1) d = sdPoly4(q) - 0.10;
    else d = length(q) - 0.36;
    return d * ptr_size;
}
float smin(float a, float b, float k) {
    if (k <= 0.01) return min(a, b);
    float h = max(k - abs(a - b), 0.0) / k;
    return min(a, b) - h * h * k * 0.25;
}
float lensSd(int i, vec2 p) {
    vec4 R = L_rect[i];
    vec2 c = (R.xy + R.zw) * 0.5, hs = (R.zw - R.xy) * 0.5;
    // Rounded circular arc: both long edges share the Blob centre. The
    // endpoint distance produces true round caps and continuous refraction.
    if (L_d[i].y < 0.0) {
        vec2 q = p - c;
        float a = L_d[i].z, half_angle = L_d[i].w;
        float theta = atan(q.y, q.x) - a;
        theta = atan(sin(theta), cos(theta));
        float nearest = a + clamp(theta, -half_angle, half_angle);
        float orbit = hs.x - L_a[i].x;
        // Keep the tube thickness constant all the way into its round caps.
        // Tapering it at the ends produced small pointed fins during the
        // palette's extrusion, which reads like spikes rather than glass.
        float radius = L_a[i].x;
        float d = length(q - orbit * vec2(cos(nearest), sin(nearest))) - radius;
        if (i == ptr_target) d = smin(d, sdPointer(p), ptr_k);
        return d;
    }
    float n = L_d[i].y >= 4.0 ? SQUIRCLE_N : L_d[i].y;            // 5 = "squircle, please"
    float k = L_d[i].y >= 4.0 ? SQUIRCLE_K : 1.0;
    float r = min(L_a[i].x * k, min(hs.x, hs.y));
    float d = sdShape(p, c, hs, r, n);
    if (i == ptr_target) d = smin(d, sdPointer(p), ptr_k);   // liquid bridge to the pointer
    return d;
}

vec2 contentUV(vec2 size, vec2 pp) {
    // content layers are bottom-anchored: the tab bar sits still while a page morphs
    vec2 q = vec2(pp.x, pp.y - (panel_size.y - size.y / ink_ss)) * ink_ss;
    if (tool_card > 0.5) q = pp * ink_ss;
    return q / size;
}
float inkAt(sampler2D t, vec2 size, vec2 pp) {
    vec2 uv = contentUV(size, pp);
    if (uv.x < 0.0 || uv.y < 0.0 || uv.x >= 1.0 || uv.y >= 1.0) return 0.0;
    return textureLod(t, uv, 0.0).r;
}
float inkHaloAt(sampler2D t, vec2 size, vec2 pp, float lod) {   // text coverage, softened
    vec2 uv = contentUV(size, pp);
    if (uv.x < -0.05 || uv.y < -0.05 || uv.x > 1.05 || uv.y > 1.05) return 0.0;
    return textureLod(t, uv, lod).r;
}
vec4 accAt(sampler2D t, vec2 size, vec2 pp) {
    vec2 uv = contentUV(size, pp);
    if (uv.x < 0.0 || uv.y < 0.0 || uv.x >= 1.0 || uv.y >= 1.0) return vec4(0.0);
    vec4 c = texture(t, uv);
    return vec4(c.rgb * c.a, c.a);      // straight alpha in, premultiplied out
}
vec4 backdropAt(sampler2D t, vec2 size, vec2 pp) {
    vec2 uv = contentUV(size, pp);
    if (uv.x < 0.0 || uv.y < 0.0 || uv.x >= 1.0 || uv.y >= 1.0) return vec4(0.0);
    vec2 q = uv - vec2(0.5);
    float c = cos(backdrop_motion.w), s = sin(backdrop_motion.w);
    q = mat2(c, -s, s, c) * q * max(backdrop_motion.x, 1.0);
    // A restrained wave keeps the cover alive without looking like liquid water.
    q.x += 0.010 * sin((uv.y + 0.15) * 6.283 + backdrop_time * 0.22);
    q.y += 0.008 * cos((uv.x - 0.10) * 5.400 - backdrop_time * 0.18);
    uv = q + vec2(0.5) + backdrop_motion.yz;
    if (uv.x < 0.0 || uv.y < 0.0 || uv.x >= 1.0 || uv.y >= 1.0) return vec4(0.0);
    vec4 c0 = texture(t, uv);
    return vec4(c0.rgb * c0.a, c0.a);    // straight alpha in, premultiplied out
}

vec4 backdropField(vec2 size, vec2 pp) {
    vec2 uv = contentUV(size, pp);
    if (uv.x < 0.0 || uv.y < 0.0 || uv.x >= 1.0 || uv.y >= 1.0) return vec4(0.0);
    float t = backdrop_time;
    float c = cos(backdrop_motion.w), s = sin(backdrop_motion.w);
    vec2 q = mat2(c, -s, s, c) * (uv - vec2(0.5)) * max(backdrop_motion.x, 1.0);
    q += vec2(0.5) + backdrop_motion.yz;
    // Low-frequency warp makes the colour masses travel continuously instead
    // of looking like a static gradient or a moving copy of the cover.
    q += 0.045 * vec2(sin(q.y * 5.4 + t * 0.19),
                      cos(q.x * 4.7 - t * 0.16));

    vec2 p0 = vec2(0.18 + 0.11 * sin(t * 0.17), 0.23 + 0.10 * cos(t * 0.13));
    vec2 p1 = vec2(0.78 + 0.12 * cos(t * 0.11), 0.22 + 0.11 * sin(t * 0.16));
    vec2 p2 = vec2(0.24 + 0.12 * cos(t * 0.14), 0.78 + 0.10 * sin(t * 0.12));
    vec2 p3 = vec2(0.77 + 0.10 * sin(t * 0.15), 0.75 + 0.12 * cos(t * 0.10));
    vec2 p4 = vec2(0.50 + 0.15 * sin(t * 0.09), 0.46 + 0.14 * cos(t * 0.12));
    vec2 p5 = vec2(0.48 + 0.18 * cos(t * 0.13), 0.30 + 0.16 * sin(t * 0.08));
    float w0 = 0.22 + 1.28 * exp(-dot(q - p0, q - p0) / 0.19);
    float w1 = 0.22 + 1.20 * exp(-dot(q - p1, q - p1) / 0.20);
    float w2 = 0.22 + 1.18 * exp(-dot(q - p2, q - p2) / 0.21);
    float w3 = 0.22 + 1.14 * exp(-dot(q - p3, q - p3) / 0.20);
    float w4 = 0.26 + 1.10 * exp(-dot(q - p4, q - p4) / 0.24);
    float w5 = 0.26 + 1.04 * exp(-dot(q - p5, q - p5) / 0.23);
    float total = w0 + w1 + w2 + w3 + w4 + w5;
    vec3 field = (backdrop_color0.rgb * w0 + backdrop_color1.rgb * w1
                + backdrop_color2.rgb * w2 + backdrop_color3.rgb * w3
                + backdrop_color4.rgb * w4 + backdrop_color5.rgb * w5) / total;
    // Keep it rich but behind the foreground type and controls.
    field = mix(vec3(luma(field) * 0.48), field, 0.90);
    return vec4(clamp(field * 0.92 + 0.015, 0.0, 1.0), 1.0);
}

void main() {
    vec2 w = gl_FragCoord.xy;
    vec2 pp = w - panel_pos;                         // panel-local px
    vec2 pc = panel_size * 0.5;
    float sdP = sdPanel(pp);
    // Analytic coverage keeps frosted edges smooth at native DPI instead of
    // producing square, one-pixel chunks when the backdrop is bright.
    float panel_aa = max(0.75, fwidth(sdP));
    float mask = 1.0 - smoothstep(-panel_aa, panel_aa, sdP);
    float sdS = sdPanel(pp - vec2(0.0, 8.0 * S));
    float shadow = 0.30 * (1.0 - smoothstep(-12.0 * S, 20.0 * S, sdS));
    bool near_lens = false;
    for (int i = 0; i < n_lens; i++) {
        vec4 R = L_rect[i];
        if (pp.x >= R.x - 1.0 && pp.y >= R.y - 1.0 &&
            pp.x <= R.z + 1.0 && pp.y <= R.w + 1.0) {
            near_lens = true;
            break;
        }
    }
    if (mask <= 0.0 && !near_lens) { frag = vec4(0.0, 0.0, 0.0, shadow); return; }

    // ── optics accumulation: the panel itself, then every control lens ──
    vec2 d = vec2(0.0);
    float frost = 0.0, lift = 0.0, raised = 0.0, fill = 0.0;
    float rim = 0.0, face = 0.0, glow = 0.0;
    vec3 tint = vec3(0.0); float tint_a = 0.0;
    float lens_mask = 0.0;
    if (magnifier_zoom > 0.0) {
        vec2 centre = (magnifier_rect.xy + magnifier_rect.zw) * .5;
        float head = (magnifier_rect.z - magnifier_rect.x) * .5 + 8.0*S;
        float rod = (1.0-smoothstep(-1.0, 1.0, sdMagnifierRod(pp))) *
                    smoothstep(head-2.0*S, head+3.0*S, length(pp-centre));
        frost = .9 * rod;
        lift = .12 * rod;
        raised = .25 * rod;
    }

    vec2 nP = nPanel(pp);
    float depth = max(-sdP, 0.0);
    // Use the older, smoother panel deformation while retaining the newer
    // border shading and optical highlight.
    float t = clamp(1.0 - depth / (28.0 * S), 0.0, 1.0) * clamp(0.5 - sdP, 0.0, 1.0);
    d += nP * (32.0 * S) * pow(t, 2.2);
    d += (pp - pc) * (0.975 - 1.0) * mask;
    float r0 = exp(-depth / (2.80 * S)) * clamp(0.9 - sdP, 0.0, 1.0);
    rim += r0 * 1.08; face += r0 * dot(nP, light); glow += pow(t, 3.0) * 0.11;

    for (int i = 0; i < n_lens; i++) {
        vec4 R = L_rect[i];
        // Extruded satellites already belong to the outer glass surface. A
        // second independent bevel would leave seams across their liquid necks.
        if (tool_expansion > 0.0 && L_d[i].y < 0.0) continue;
        if (i != ptr_target && (pp.x < R.x - 1.0 || pp.y < R.y - 1.0 || pp.x > R.z + 1.0 || pp.y > R.w + 1.0))
            continue;
        vec4 A = L_a[i], B = L_b[i], C = L_c[i], D = L_d[i];
        vec2 c = (R.xy + R.zw) * 0.5;
        float sd = lensSd(i, pp);
        float lens_aa = max(D.y < 0.0 ? 1.25 : 0.75, fwidth(sd));
        float cov = 1.0 - smoothstep(-lens_aa, lens_aa, sd);
        lens_mask = max(lens_mask, cov);
        if (cov <= 0.0 && sd > 0.8) continue;
        vec2 g = vec2(lensSd(i, pp + vec2(0.5, 0.0)) - lensSd(i, pp - vec2(0.5, 0.0)),
                      lensSd(i, pp + vec2(0.0, 0.5)) - lensSd(i, pp - vec2(0.0, 0.5)));
        vec2 n = g / max(length(g), 1e-5);
        float dep = max(-sd, 0.0);
        float tt = clamp(1.0 - dep / max(A.z, 1.0), 0.0, 1.0) * clamp(0.5 - sd, 0.0, 1.0);
        d += n * A.y * 1.10 * pow(tt, 2.1);
        d += (pp - c) * (A.w - 1.0) * cov;
        float rr = exp(-dep / (1.45 * S)) * clamp(0.9 - sd, 0.0, 1.0) * B.x * 1.06;
        rim += rr; face += rr * dot(n, light); glow += pow(tt, 3.0) * 0.10 * B.x;
        frost = max(frost, cov * B.y);
        lift = max(lift, cov * B.z);
        raised = max(raised, cov * B.w);
        fill = max(fill, cov * D.x);
        float ta = cov * C.a;
        tint = mix(tint, C.rgb, ta > 0.0 ? ta / max(tint_a + ta, 1e-4) : 0.0);
        tint_a = max(tint_a, ta);
    }
    // Controls can float on the transparent satellite canvas outside the
    // unchanged main bubble. Their own lens coverage is the alpha in that
    // region; the panel mask still wins everywhere inside the bubble.
    mask = max(mask, lens_mask);
    if (tool_card > 0.5) mask = 1.0 - smoothstep(-panel_aa, panel_aa, sdP);

    // ── the free-floating glass pointer ──
    if (ptr_alpha > 0.01 && ptr_target < 0) {
        float sd = sdPointer(pp);
        float cov = clamp(0.5 - sd, 0.0, 1.0) * ptr_alpha;
        if (cov > 0.0 || sd < 0.8) {
            vec2 g = vec2(sdPointer(pp + vec2(0.5, 0.0)) - sdPointer(pp - vec2(0.5, 0.0)),
                          sdPointer(pp + vec2(0.0, 0.5)) - sdPointer(pp - vec2(0.0, 0.5)));
            vec2 n = g / max(length(g), 1e-5);
            float dep = max(-sd, 0.0);
            float tt = clamp(1.0 - dep / (0.34 * ptr_size), 0.0, 1.0) * clamp(0.5 - sd, 0.0, 1.0);
            d += n * (0.55 * ptr_size) * pow(tt, 1.8) * ptr_alpha;
            float rr = exp(-dep / S) * clamp(0.9 - sd, 0.0, 1.0) * ptr_alpha;
            rim += rr; face += rr * dot(n, light); glow += pow(tt, 3.0) * 0.12 * ptr_alpha;
            frost = max(frost, 0.3 * cov);
            lift = max(lift, 0.34 * cov);
            raised = max(raised, cov);
        }
    }

    // ── glass spectrum: each bar is a small capsule lens ──
    float viz_coverage = 0.0;
    if (viz_alpha > 0.0 && viz_rect.z > viz_rect.x && pp.x > viz_rect.x && pp.x < viz_rect.z
            && pp.y > viz_rect.y && pp.y < viz_rect.w) {
        float pitch = (viz_rect.z - viz_rect.x) / 28.0;
        int i = clamp(int((pp.x - viz_rect.x) / pitch), 0, 27);
        float cx = viz_rect.x + (float(i) + 0.5) * pitch;
        float r = pitch * 0.32;
        float bottom = viz_rect.w - r - 1.0;
        float top = bottom - clamp(bands[i] * viz_alpha, 0.0, 1.0) * (viz_rect.w - viz_rect.y - 2.0 * r - 2.0);
        vec2 q = vec2(pp.x - cx, pp.y - clamp(pp.y, top, bottom));
        float dist = length(q);
        float din = r - dist;
        float cov = clamp(din + 0.5, 0.0, 1.0) * viz_alpha;
        viz_coverage = cov;
        if (cov > 0.0) {
            vec2 n = dist > 1e-3 ? q / dist : vec2(0.0);
            float tt = clamp(1.0 - din / (r * 0.95), 0.0, 1.0);
            d += n * (6.0 * S) * tt * tt * cov;
            frost = max(frost, 0.75 * cov);
            lift = max(lift, 0.20 * cov);
            raised = max(raised, cov);
            float rr = exp(-max(din, 0.0) / S) * cov;
            rim += rr; face += rr * dot(n, light);
        }
    }

    // ── sample the scene through the glass ──
    vec2 p = w + cap_origin;
    // Split the colour samples only where a glass edge is present. The clear
    // centre gets one aligned sample per channel, while raised borders carry
    // the restrained chromatic fringe.
    float edge_chroma = smoothstep(0.08, 0.72, clamp(rim, 0.0, 1.0));
    float chroma = 0.17 * edge_chroma;
    vec3 clear = vec3(at(p + d * (1.0 - chroma), 0.5).r,
                      at(p + d, 0.5).g,
                      at(p + d * (1.0 + chroma), 0.5).b);
    // Keep frost readable without turning white scenery into a clipped block.
    // Low glassiness is genuinely clear: do not force a full-panel blur just
    // because the glass control is slightly above zero.
    float global_frost = smoothstep(0.16, 0.88, glassiness) * 0.72;
    // Busy scenery (windows full of text, mixed black and white UI) shows through
    // light frost as sharp shapes that compete with Blob's own words. Measure the
    // fine detail behind the whole panel and blur harder when it is busy; calm
    // wallpapers stay clear. One panel-wide value keeps the frost even: a
    // per-pixel estimate flickers along glyph edges and costs every pixel.
    // A coarse map measured on the CPU per capture, eased over time and read
    // with linear filtering, so frost follows a busy window smoothly.
    float busy = texture(busy_map, clamp(pp / panel_size, 0.0, 1.0)).r;
    // Glassiness 0 is true clear glass and stays clear over anything; the extra
    // frost over busy scenery fades in with the setting.
    busy *= smoothstep(0.05, 0.35, glassiness);
    float frost_amt = min(max(max(frost, global_frost), busy * 0.96), mix(0.82, 0.96, busy));
    vec3 col = clear;
    if (frost_amt > 0.001) {
        // Real frosted glass: one smooth Gaussian of the scenery (a pre-pass whose
        // radius follows the glassiness setting), never scattered taps, which
        // read as blotches and bright patches on detailed backgrounds.
        vec3 frosted = textureLod(bgblur, (p + d) / bg_size, 0.0).bgr;
        // Busy scenery: also draw the blur's black/white extremes toward the
        // area's average so text-heavy windows become an even haze.
        frosted = mix(frosted, mix(frosted, at(p, 7.5), 0.35), busy);
        col = mix(clear, frosted, frost_amt);
    }
    float l = luma(col);
    col = mix(vec3(l), col, 1.06);

    // text colour follows the scenery behind each spot; the glass leans away from the ink
    float local = luma(at(p, 4.5));
    float dark = smoothstep(0.50, 0.66, local);
    float push = 0.10 + 0.32 * glassiness;
    col = mix(col, vec3(0.0), push * (1.0 - dark));
    col = mix(col, vec3(1.0), (push + 0.08) * dark);
    col = mix(col, mix(ambient.rgb, ambient2, clamp(pp.y / panel_size.y, 0.0, 1.0)), ambient.a);

    // frosted controls: milky on dark scenery; on bright scenery tracks sink, raised parts glow
    vec3 tint_dark_scene = vec3(0.93);
    vec3 tint_bright_scene = mix(vec3(0.08), vec3(0.78), raised);
    float amt = mix(lift, mix(lift * 1.2, 0.55 + lift, raised), dark);
    col = mix(col, mix(tint_dark_scene, tint_bright_scene, dark), clamp(amt * 0.72, 0.0, 0.72));
    col = mix(col, tint * mix(1.0, 0.85, dark), clamp(tint_a, 0.0, 1.0));

    vec4 backdrop = mix(backdropField(ink0_size, pp),
                        backdropField(ink1_size, pp), fade);
    col = mix(col, backdrop.rgb, backdrop_alpha);
    vec4 pic = mix(accAt(pic0, ink0_size, pp), accAt(pic1, ink1_size, pp), fade);
    col = col * (1.0 - pic.a) + pic.rgb;

    float spec = rim * 0.075 + 0.36 * max(face, 0.0) + 0.14 * max(-face, 0.0);
    spec += glow * 0.55 * clamp(-dot(d, light) / (20.0 * S), 0.0, 1.0);
    col += min(spec, 0.22) + 0.015;
    col -= rim * 0.13 * dark;

    // Read the real desktop at full resolution inside the magnifier. Frost,
    // colour shifts and decorative refraction stay on the surrounding frame:
    // applying them to the text itself would defeat the reading tool.
    if (magnifier_zoom > 0.0) {
        vec2 centre = (magnifier_rect.xy + magnifier_rect.zw) * 0.5;
        float radius = (magnifier_rect.z - magnifier_rect.x) * .5;
        vec2 q = pp - centre;
        float dist = length(q);
        vec2 normal = q / max(dist, .001);
        float aperture = dist - radius;
        float coverage = 1.0 - smoothstep(-1.0, 1.0, aperture);
        float edge = smoothstep(radius-14.0*S, radius, dist);
        vec2 source = panel_pos + cap_origin + centre + q / magnifier_zoom;
        source += normal * 10.0*S * edge * edge;
        vec2 split = normal * .95*S * edge * edge;
        vec3 reading = vec3(at(source+split, 0.0).r, at(source, 0.0).g, at(source-split, 0.0).b);
        col = mix(col, reading, coverage);
    }

    // Text must contrast with the glass it sits on. A white-to-black blend by the scenery gave
    // grey text on mid-tone wallpapers, so ink is pure white or near-black, chosen from the glass
    // estimated across the panel's width at this row (a vertical wallpaper edge never splits a
    // word), or from the scenery right here where it differs sharply (a white window beside a
    // dark wallpaper). Between 0.44 and 0.52 luma either ink reaches >= 3.75:1, so there every
    // row follows one panel-wide choice; lines only switch where the glass is clearly lighter
    // or darker, never mid-line on a gentle gradient. Edges are antialiased over one pixel.
    float row_l = 0.0, panel_l = 0.0;
    for (int k = 0; k < 5; k++) {
        float fx = panel_pos.x + panel_size.x * (0.1 + 0.2 * float(k));
        row_l += luma(at(vec2(fx, w.y) + cap_origin, 7.0)) * 0.2;
        for (int j = 0; j < 3; j++)
            panel_l += luma(at(vec2(fx, panel_pos.y + panel_size.y * (0.2 + 0.3 * float(j))) + cap_origin, 7.0)) / 15.0;
    }
    float here_l = luma(at(p, 6.0));
    float region = mix(row_l, here_l, smoothstep(0.12, 0.22, abs(here_l - row_l)));
    // Buttons carry their own frosted/colour tint (above); estimate it from the same row-level
    // scenery so a label reads against the button it sits on, not the bare glass.
    float rd = smoothstep(0.50, 0.66, region);
    float glass_l = mix(glassOver(region, push), luma(mix(tint_dark_scene, tint_bright_scene, rd)),
                        clamp(mix(lift, mix(lift * 1.2, 0.55 + lift, raised), rd) * 0.72, 0.0, 0.72));
    glass_l = mix(glass_l, luma(tint * mix(1.0, 0.85, rd)), clamp(tint_a, 0.0, 1.0));
    float glass_aa = max(fwidth(glass_l), 1e-4);
    float clearly_light = smoothstep(0.52 - glass_aa, 0.52 + glass_aa, glass_l);
    float clearly_dark = 1.0 - smoothstep(0.44 - glass_aa, 0.44 + glass_aa, glass_l);
    dark = mix(mix(step(0.48, glassOver(panel_l, push)), 1.0, clearly_light), 0.0, clearly_dark);

    // Album artwork is opaque: text must contrast with the cover, not the desktop.
    float ink_dark = mix(dark, smoothstep(.45, .65, dot(col, vec3(.2126, .7152, .0722))), pic.a);
    vec3 ink_col = mix(vec3(1.0), vec3(0.07), ink_dark);
    // Legibility halo: where the glass right under the text is too close to the ink colour
    // (a thin bright streak refracted under white text), lean the glass around the glyphs away
    // from the ink, like a vibrancy backing. Elsewhere it stays off, so the glass is unchanged.
    float halo_lod = 2.6 + log2(max(S, 1.0));
    float halo = clamp(mix(inkHaloAt(ink0, ink0_size, pp, halo_lod), inkHaloAt(ink1, ink1_size, pp, halo_lod), fade) * 2.4, 0.0, 1.0);
    float risk = 1.0 - smoothstep(0.22, 0.42, abs(luma(col) - luma(ink_col)));
    col = mix(col, vec3(1.0) - ink_col * 0.9, halo * mix(0.45, 0.85, risk) * risk * (1.0 - pic.a));
    // Opaque covers hide desktop refraction: retain readable frosted bars above art.
    col = mix(col, ink_col, viz_coverage * pic.a * .72);
    col = mix(col, ink_col, fill * 0.85);
    float ia = mix(inkAt(ink0, ink0_size, pp), inkAt(ink1, ink1_size, pp), fade);
    col = mix(col, ink_col, ia);
    vec4 ac = mix(accAt(acc0, ink0_size, pp), accAt(acc1, ink1_size, pp), fade);
    if (ac.a > 0.0) {
        vec3 hue = ac.rgb / ac.a;
        col = mix(col, mix(hue, hue * 0.62, dark), ac.a);
    }
    col = clamp(col, 0.0, 1.0) * mask;
    float alpha = mask + shadow * (1.0 - mask);
    frag = vec4(col.b, col.g, col.r, alpha);   // BGRA, premultiplied — ready for the DIB
}
"""


# Mean |fine - block mean| luma of the scenery behind the panel: at or below
# BUSY_CALM the glass keeps its normal frost, at BUSY_FULL it is fully backed.
BUSY_CALM, BUSY_FULL = 0.02, 0.06


class ScreenSource:
    """What's behind the panel, via DXGI Desktop Duplication (GPU-side, ~0.7 ms, and it tells us
    when nothing changed). Excluded-from-capture windows — ours — never show up in it.
    Falls back to GDI BitBlt if duplication isn't available."""

    def __init__(self):
        self.outputs = []  # (left, top, right, bottom, camera)
        self.frames = {}
        self.frozen = False
        self.screen_dc = user32.GetDC(None)
        try:
            import dxcam
            for i in range(8):
                try:
                    cam = dxcam.create(output_idx=i, output_color="BGRA")
                except Exception:
                    break
                if cam is None:
                    break
                r = cam._output.desc.DesktopCoordinates
                self.outputs.append((r.left, r.top, r.right, r.bottom, cam))
        except Exception:
            self.outputs = []

    def grab(self, x, y, w, h, out, moved):
        """Fill out[:h, :w] with the screen rect at (x, y) if anything changed. Returns True when the
        pixels are new. Only the panel's own rectangle is copied out of the GPU frame."""
        x, y = int(round(x)), int(round(y))
        w, h = max(1, int(round(w))), max(1, int(round(h)))
        if self.frozen:
            return False
        cx, cy = x + w // 2, y + h // 2
        for l, t, r, b, cam in self.outputs:
            if l <= cx < r and t <= cy < b:
                break
        else:
            gdi_grab(self.screen_dc, x, y, w, h, out)
            return True
        sx0, sy0, sx1, sy1 = max(x, l), max(y, t), min(x + w, r), min(y + h, b)
        if (sx0, sy0, sx1, sy1) != (x, y, x + w, y + h):
            # A single DXGI output cannot fill a region crossing monitors. Do not
            # leave the other monitor's pixels stale in the capture buffer.
            gdi_grab(self.screen_dc, x, y, w, h, out)
            return True
        f = None
        if sx1 > sx0 and sy1 > sy0:
            try:
                # Read the newest ready DXGI frame without waiting for the
                # next present. Waiting here makes the glass visibly trail a
                # scrolling or moving window by roughly one display frame.
                f = cam.grab(region=(sx0 - l, sy0 - t, sx1 - l, sy1 - t),
                            copy=False, new_frame_only=False)
            except Exception:
                f = None
        fresh = bool(getattr(getattr(cam, "_duplicator", None), "updated", True))
        if f is not None and (fresh or moved):
            out[sy0 - y:sy1 - y, sx0 - x:sx1 - x] = f
            return True
        if moved:  # the desktop didn't change but we moved: grab the new spot directly
            gdi_grab(self.screen_dc, x, y, w, h, out)
            return True
        return False


_gdi_dib = None


def gdi_grab(screen_dc, x, y, w, h, out):
    global _gdi_dib
    x, y = int(round(x)), int(round(y))
    w, h = max(1, int(round(w))), max(1, int(round(h)))
    d = _gdi_dib
    if d is None or d.w < w or d.h < h:
        replacement = Dib(max(w, d.w if d else 0), max(h, d.h if d else 0))
        if d is not None:
            d.free()
        d = _gdi_dib = replacement
    gdi32.BitBlt(d.dc, 0, 0, w, h, screen_dc, x, y, 0x00CC0020)
    out[:h, :w] = d.arr[:h, :w]


class GlassRenderer:
    """Window buffer is fixed (max panel height); the panel sits bottom-anchored inside it and
    can change height every frame."""

    def __init__(self, scale, width, max_height):
        import moderngl
        self.S = scale
        self.sp = int(28 * scale)        # shadow room around the panel
        self.M = int(52 * scale)         # extra background grabbed so the thicker edge can refract outside
        self.w, self.hmax = width, max_height
        self.W, self.H = width + 2 * self.sp, max_height + 2 * self.sp
        self.screen_dc = user32.GetDC(None)
        self.dib = Dib(self.W, self.H)
        self.cap = np.zeros((max_height + 2 * self.M, width + 2 * self.M, 4), np.uint8)
        self.source = ScreenSource()
        self.dump_path = None
        self.panel_side = "right"
        self._last_key = None
        self.stats = {}
        self.ctx = moderngl.create_standalone_context()
        self.prog = self.ctx.program(vertex_shader=VERT, fragment_shader=FRAG)
        vbo = self.ctx.buffer(np.array([-1, -1, 3, -1, -1, 3], np.float32).tobytes())
        self.vao = self.ctx.vertex_array(self.prog, [(vbo, "2f", "pos")])
        self.fbo = self.ctx.simple_framebuffer((self.W, self.H), components=4)
        self.bg = self.ctx.texture((self.cap.shape[1], self.cap.shape[0]), 4, dtype="f1")
        self.bg.build_mipmaps()
        self.bg.filter = (self.ctx.LINEAR_MIPMAP_LINEAR, self.ctx.LINEAR)
        self.bg.repeat_x = self.bg.repeat_y = False
        blur_size = (max(1, self.cap.shape[1] // 4), max(1, self.cap.shape[0] // 4))
        self.blur_prog = self.ctx.program(vertex_shader=VERT, fragment_shader=BLUR_FRAG)
        self.blur_vao = self.ctx.vertex_array(self.blur_prog, [(vbo, "2f", "pos")])
        self.blur_tex = [self.ctx.texture(blur_size, 4, dtype="f1") for _ in range(2)]
        for tex in self.blur_tex:
            tex.filter = (self.ctx.LINEAR, self.ctx.LINEAR)
            tex.repeat_x = tex.repeat_y = False
        self.blur_fbo = [self.ctx.framebuffer(color_attachments=[tex]) for tex in self.blur_tex]
        self.blur_prog["out_size"].value = (float(blur_size[0]), float(blur_size[1]))
        self._blur_dirty = True
        self._blur_sigma = 0.0
        self.inks = [None, None]
        self.accs = [None, None]
        self.pics = [None, None]
        self.backdrops = [None, None]
        # The app's 16 ms native timer is the 60 FPS presentation rail. Give
        # the capture gate a small tolerance below 1/60 so consecutive timer
        # ticks do not alternate into a visibly choppy 30 FPS cadence.
        # Keep the slower fallback conservative so it does not steal CPU.
        self.capture_min_interval = (1.0 / 64.0
                                     if getattr(self.source, "outputs", ()) else 1.0 / 30.0)
        self._last_capture = 0.0
        self._bg_dirty = True
        self._bg_uploaded = (0, 0)
        self._dib_rect = None
        # Framebuffer readback is tightly packed to the requested viewport;
        # keep a 1-D staging buffer so rows never inherit the DIB's wider
        # stride when the panel is narrower than the fixed window.
        self._readback = np.empty(self.W * self.H * 4, dtype=np.uint8)
        units = {"bg": 0, "ink0": 1, "ink1": 2, "acc0": 3, "acc1": 4,
                 "pic0": 5, "pic1": 6, "bgblur": 9, "busy_map": 10}
        for name, unit in units.items():
            self.prog[name].value = unit
        self.prog["bg_size"].value = (self.cap.shape[1], self.cap.shape[0])
        self.prog["S"].value = float(scale)
        self.prog["panel_r"].value = float(34 * scale)
        self.prog["panel_morph"].value = 0.0
        self.prog["bubble_audio"].value = (0.0, 0.0, 0.0)
        self.prog["bubble_side"].value = 1.0
        self.prog["bubble_time"].value = 0.0
        self.prog["bubble_proximity"].value = 0.0
        self.prog["tool_expansion"].value = 0.0
        self.busy_tex = None
        self._busy = self._busy_target = np.zeros((1, 1), np.float32)
        self._busy_t = time.perf_counter()
        self._upload_busy(self._busy)
        self.set_tool_card(False)
        self.set_magnifier(None)
        self.prog["backdrop_motion"].value = (1.0, 0.0, 0.0, 0.0)
        self.prog["backdrop_time"].value = 0.0
        self.prog["backdrop_alpha"].value = 0.0
        self._backdrop_palette = None
        self.set_backdrop_palette(None)
        self.prog["viz_alpha"].value = 0.0
        self.prog["n_lens"].value = 0
        self.prog["ambient"].value = (0.0, 0.0, 0.0, 0.0)
        self.prog["ink_ss"].value = 1.0
        self.prog["ambient2"].value = (0.0, 0.0, 0.0)
        self.prog["ptr_target"].value = -1
        self.prog["ptr_alpha"].value = 0.0
        self.prog["ptr_size"].value = float(19 * scale)
        self.prog["ptr_shape"].value = 0
        self.set_content(np.zeros((4, 4), np.uint8), np.zeros((4, 4, 4), np.uint8), None, instant=True)

    def panel_y(self, h):
        return self.H - self.sp - h

    def panel_x(self, w):
        if getattr(self, "panel_side", "right") == "left":
            return self.sp
        return self.W - self.sp - int(round(w))   # right-aligned, so a narrower panel stays in the corner

    def _textures(self, ink, accent, pic, backdrop=None):
        """Layers go up as plain bytes — no float maths, no premultiply on the CPU."""
        ink = np.ascontiguousarray(np.asarray(ink, np.uint8))
        h, w = ink.shape
        out = [ink]
        for layer in (accent, pic, backdrop):
            a = np.zeros((h, w, 4), np.uint8) if layer is None else np.ascontiguousarray(
                np.asarray(layer, np.uint8))
            out.append(a)
        return out

    def set_content(self, ink, accent, pic=None, backdrop=None, instant=False):
        """New content (text, accents, images). The previous one is kept for crossfading."""
        ink, acc, pic, backdrop = self._textures(ink, accent, pic, backdrop)
        h, w = ink.shape
        new = []
        for data, comps in ((ink, 1), (acc, 4), (pic, 4), (backdrop, 4)):
            t = self.ctx.texture((w, h), comps, data.tobytes(), alignment=1)
            t.filter = (self.ctx.LINEAR, self.ctx.LINEAR)
            if comps == 1:   # ink: mipmaps feed the legibility halo; inkAt still reads level 0
                t.build_mipmaps()
            new.append(t)
        layers = [self.inks, self.accs, self.pics, self.backdrops]
        for lst, t in zip(layers, new):
            if instant:
                for old in {id(x): x for x in lst if x is not None}.values():
                    old.release()
                lst[0] = lst[1] = t
            else:
                if lst[0] is not None and lst[0] is not lst[1]:
                    lst[0].release()
                lst[0], lst[1] = lst[1], t
        self._last_key = None

    def replace_content(self, ink, accent, pic=None, backdrop=None):
        """Refresh the current content in place (live numbers) without a crossfade."""
        ink_a, acc_a, pic_a, backdrop_a = self._textures(ink, accent, pic, backdrop)
        h, w = ink_a.shape[:2]
        if self.inks[1] is None or self.inks[1].size != (w, h):
            return self.set_content(ink, accent, pic, backdrop, instant=True)
        self.inks[1].write(ink_a.tobytes())
        self.inks[1].build_mipmaps()
        self.accs[1].write(acc_a.tobytes())
        self.pics[1].write(pic_a.tobytes())
        self.backdrops[1].write(backdrop_a.tobytes())
        self._last_key = None

    def set_panel_shape(self, morph):
        self.prog["panel_morph"].value = float(max(0.0, min(1.0, morph)))

    def set_panel_side(self, side):
        self.panel_side = "left" if side == "left" else "right"
        self.prog["bubble_side"].value = -1.0 if self.panel_side == "left" else 1.0

    def set_bubble_time(self, seconds):
        self.prog["bubble_time"].value = float(seconds)

    def set_bubble_proximity(self, value):
        self.prog["bubble_proximity"].value = float(max(0.0, min(1.0, value)))

    def set_tool_card(self, active):
        self.prog["tool_card"].value = float(bool(active))

    def set_tool_expansion(self, value):
        """Keep the original bubble fixed while the transparent tool canvas grows."""
        self.prog["tool_expansion"].value = float(max(0.0, min(1.0, value)))

    def set_magnifier(self, rect, zoom=2.0):
        self.prog["magnifier_rect"].value = tuple(float(v) for v in (rect or (0, 0, 0, 0)))
        self.prog["magnifier_zoom"].value = float(max(0.25, min(40.0, zoom))) if rect else 0.0

    def set_bubble_pulse(self, pulse):
        value = float(max(0.0, min(1.0, pulse)))
        self.prog["bubble_audio"].value = (value, value, value)

    def set_bubble_audio(self, bass, mid, treble):
        clamp = lambda value: float(max(0.0, min(1.0, value)))
        self.prog["bubble_audio"].value = (clamp(bass), clamp(mid), clamp(treble))

    def set_backdrop_palette(self, palette):
        """Upload six album-derived colours only when the cover changes."""
        fallback = (
            (0.12, 0.15, 0.20, 1.0), (0.18, 0.16, 0.22, 1.0),
            (0.12, 0.20, 0.24, 1.0), (0.22, 0.18, 0.14, 1.0),
            (0.16, 0.20, 0.28, 1.0), (0.20, 0.14, 0.20, 1.0),
        )
        colors = tuple(tuple(float(v) for v in color[:4]) for color in (palette or fallback))
        colors = (colors + fallback)[:6]
        if colors == self._backdrop_palette:
            return
        self._backdrop_palette = colors
        for index, color in enumerate(colors):
            self.prog[f"backdrop_color{index}"].value = color

    def set_backdrop_motion(self, scale=1.0, drift_x=0.0, drift_y=0.0,
                            rotation=0.0, seconds=0.0, alpha=0.0):
        self.prog["backdrop_motion"].value = (float(max(1.0, scale)), float(drift_x),
                                               float(drift_y), float(rotation))
        self.prog["backdrop_time"].value = float(seconds)
        self.prog["backdrop_alpha"].value = float(max(0.0, min(1.0, alpha)))

    def set_supersample(self, ss):
        self.prog["ink_ss"].value = float(ss)

    def set_ambient(self, rgba, bottom=None):
        self.prog["ambient"].value = tuple(float(v) for v in rgba)
        self.prog["ambient2"].value = tuple(float(v) for v in (bottom or rgba[:3]))

    def set_lenses(self, lenses):
        """lenses: list of dicts with rect, r, strength, bevel, zoom, rim, frost, lift, raised,
        tint (r,g,b,a 0..1), fill, n."""
        n = min(len(lenses), MAX_LENSES)
        arr = np.zeros((5, MAX_LENSES, 4), np.float32)
        for i, L in enumerate(lenses[:n]):
            arr[0, i] = L["rect"]
            arr[1, i] = (L.get("r", 0), L.get("strength", 0), L.get("bevel", 1), L.get("zoom", 1.0))
            arr[2, i] = (L.get("rim", 0), L.get("frost", 0), L.get("lift", 0), L.get("raised", 0))
            arr[3, i] = L.get("tint", (0, 0, 0, 0))
            arc = L.get("arc")
            arr[4, i] = (L.get("fill", 0), -1.0 if arc else L.get("n", 2.0),
                         arc[0] if arc else 0, arc[1] if arc else 0)
        for k, name in enumerate(("L_rect", "L_a", "L_b", "L_c", "L_d")):
            self.prog[name].write(arr[k].tobytes())
        self.prog["n_lens"].value = n

    def set_pointer(self, pos, alpha, target, k, shape=0):
        """pos: tip in panel px; target: index into the current lens list to fuse with, or -1."""
        pr = self.prog
        pr["ptr_shape"].value = int(shape)
        pr["ptr_pos"].value = (float(pos[0]), float(pos[1])) if pos else (-999.0, -999.0)
        pr["ptr_alpha"].value = float(alpha)
        pr["ptr_target"].value = int(target)
        pr["ptr_k"].value = float(k)

    def set_viz(self, rect, bands, alpha):
        if rect is None or alpha <= 0.001:
            self.prog["viz_alpha"].value = 0.0
            return
        self.prog["viz_rect"].value = tuple(float(v) for v in rect)
        self.prog["bands"].value = tuple(float(b) for b in bands)
        self.prog["viz_alpha"].value = float(alpha)

    def capture(self, win_x, win_y, panel_w, panel_h, force=False, panel_y=None):
        """Refresh the background copy. Returns True if a new frame should be drawn."""
        # Dragging and side-preserving springs can leave the host position as a
        # float. Desktop capture APIs are integer-pixel APIs; normalize once at
        # the renderer boundary so GDI, DXGI regions and layered-window updates
        # all receive the same stable coordinates.
        win_x, win_y = int(round(win_x)), int(round(win_y))
        sp, M = self.sp, self.M
        h = int(round(panel_h))
        py = self.panel_y(h) if panel_y is None else int(round(panel_y))
        ch, cw = h + 2 * M, self.cap.shape[1]
        self.stats = getattr(self, "stats", {})
        tb = time.perf_counter()
        key = (win_x, win_y, h, int(round(panel_w)))
        moved = key != self._last_key
        now = time.perf_counter()
        # Moving a layered window can generate mouse events much faster than
        # the compositor.  Keep the desktop readback/upload inside the same
        # capture budget whether geometry changed or not; the cached scene is
        # still presented at 60 FPS while the panel glides.
        if (not force and self.capture_min_interval > 0 and
                now - self._last_capture < self.capture_min_interval):
            self.stats["capture"] = now - tb
            return False
        self._last_capture = now
        changed = self.source.grab(win_x + sp - M, win_y + py - M, cw, ch, self.cap, moved)
        self._last_key = key
        self._bg_dirty = moved or changed
        self.stats["capture"] = time.perf_counter() - tb
        return force or moved or changed

    def _blur_background(self):
        """Two-pass Gaussian of the capture into ``blur_tex[1]`` (~0.2 ms on the GPU).

        Runs only when the capture or the frost radius changed.
        """
        size = self.blur_tex[0].size
        bp = self.blur_prog
        self.blur_fbo[0].use()
        self.bg.use(0)
        bp["src"].value = 0
        bp["sigma"].value = float(max(0.5, self._blur_sigma))
        bp["src_lod"].value = 2.0
        bp["step_uv"].value = (1.0 / size[0], 0.0)
        self.blur_vao.render(mode=self.ctx.TRIANGLES)
        self.blur_fbo[1].use()
        self.blur_tex[0].use(0)
        bp["src_lod"].value = 0.0
        bp["step_uv"].value = (0.0, 1.0 / size[1])
        self.blur_vao.render(mode=self.ctx.TRIANGLES)
        self._blur_dirty = False

    @staticmethod
    def scene_busy(cap, x, y, w, h):
        """Map of fine detail (text, UI edges) behind the panel, 0..1 per ~32 px cell.

        A strided sample of the capture: each 4-px sample is compared with the
        mean of its 32-px cell. The map is widened by one cell and softened so
        frost starts just before a busy window's edge. ~0.5 ms per capture.
        """
        region = cap[max(0, y):max(0, y) + max(0, h):4, max(0, x):max(0, x) + max(0, w):4, :3]
        rows, cols = (region.shape[0] // 8) * 8, (region.shape[1] // 8) * 8
        if rows < 8 or cols < 8:
            return np.zeros((1, 1), np.float32)
        lum = region[:rows, :cols].astype(np.float32) @ np.array([0.0722, 0.7152, 0.2126], np.float32) / 255.0
        cells = lum.reshape(rows // 8, 8, cols // 8, 8)
        detail = np.abs(cells - cells.mean(axis=(1, 3), keepdims=True)).mean(axis=(1, 3))
        t = np.clip((detail - BUSY_CALM) / (BUSY_FULL - BUSY_CALM), 0.0, 1.0)
        t = t * t * (3.0 - 2.0 * t)
        pad = np.pad(t, 1, mode="edge")
        grown = np.max([pad[i:i + t.shape[0], j:j + t.shape[1]] for i in range(3) for j in range(3)], axis=0)
        pad = np.pad(grown, 1, mode="edge")
        soft = np.mean([pad[i:i + t.shape[0], j:j + t.shape[1]] for i in range(3) for j in range(3)], axis=0)
        return soft.astype(np.float32)

    def _upload_busy(self, busy):
        """Upload the eased busy map (row 0 = top, like the capture and ``pp``)."""
        data = np.ascontiguousarray(busy, np.float32)
        size = (data.shape[1], data.shape[0])
        if self.busy_tex is None or self.busy_tex.size != size:
            if self.busy_tex is not None:
                self.busy_tex.release()
            self.busy_tex = self.ctx.texture(size, 1, dtype="f4")
            self.busy_tex.filter = (self.ctx.LINEAR, self.ctx.LINEAR)
            self.busy_tex.repeat_x = self.busy_tex.repeat_y = False
        self.busy_tex.write(data.tobytes())

    def render(self, hwnd, win_x, win_y, panel_w, panel_h, fade, light, glassiness, panel_y=None):
        win_x, win_y = int(round(win_x)), int(round(win_y))
        S, sp, M = self.S, self.sp, self.M
        h, pw = int(round(panel_h)), int(round(panel_w))
        py = self.panel_y(h) if panel_y is None else int(round(panel_y))
        px = self.panel_x(pw)
        x0, y0 = max(0, px - sp), max(0, py - sp)
        x1, y1 = min(self.W, px + pw + sp), min(self.H, py + h + sp)
        dib_rect = (x0, y0, x1, y1)
        if dib_rect != self._dib_rect:
            # The layered window keeps a persistent DIB. Clear the previous
            # visible region before drawing the next one so a shrinking or
            # moving panel cannot leave a stale shadow behind.
            if self._dib_rect is None:
                self.dib.arr[:] = 0
            else:
                ox0, oy0, ox1, oy1 = self._dib_rect
                self.dib.arr[oy0:oy1, ox0:ox1] = 0
            self._dib_rect = dib_rect
        cap = self.cap
        ch, cw = h + 2 * M, cap.shape[1]
        t0 = time.perf_counter()
        if self._bg_dirty or self._bg_uploaded != (cw, ch):
            self._busy_target = self.scene_busy(cap, px + M - sp, M, pw, h)
            self.bg.write(cap[:ch], viewport=(0, 0, cw, ch))
            self.bg.build_mipmaps()
            self._bg_dirty = False
            self._blur_dirty = True
        # Frost radius follows the glassiness setting and grows over busy scenery:
        # about 6 px for light glass up to 22 px for fully frosted / text behind.
        g = float(glassiness)
        busy_weight = min(1.0, max(0.0, (g - 0.05) / 0.30))   # matches the shader's fade-in
        heavy = max(min(1.0, max(0.0, (g - 0.16) / 0.72)),
                    busy_weight * max(float(self._busy_target.max()), float(self._busy.max())))
        sigma = (6.0 + 16.0 * heavy) * self.S / 4.0
        if abs(sigma - self._blur_sigma) > 0.02:
            self._blur_sigma, self._blur_dirty = sigma, True
        if self._blur_dirty:
            self._blur_background()
            self._bg_uploaded = (cw, ch)
        self.bg.use(0)
        self.blur_tex[1].use(9)
        self.inks[0].use(1)
        self.inks[1].use(2)
        self.accs[0].use(3)
        self.accs[1].use(4)
        self.pics[0].use(5)
        self.pics[1].use(6)
        pr = self.prog
        pr["ink0_size"].value = self.inks[0].size
        pr["ink1_size"].value = self.inks[1].size
        pr["fade"].value = float(fade)
        pr["cap_origin"].value = (float(M - sp), float(M - py))
        pr["panel_pos"].value = (float(px), float(py))
        pr["panel_size"].value = (float(pw), float(h))
        pr["light"].value = tuple(float(v) for v in light)
        pr["glassiness"].value = float(glassiness)
        # Ease the busy estimate so dragging across windows fades the frost in
        # and out instead of popping it frame to frame.
        now = time.perf_counter()
        step = min(1.0, (now - self._busy_t) * 5.0)
        self._busy_t = now
        if self._busy.shape != self._busy_target.shape:
            self._busy = self._busy_target.copy()   # panel resized: no stale cells to ease from
            self._upload_busy(self._busy)
        elif step > 0 and float(np.abs(self._busy_target - self._busy).max()) > 1e-3:
            self._busy = self._busy + (self._busy_target - self._busy) * step
            self._upload_busy(self._busy)   # settled maps are not re-uploaded
        self.busy_tex.use(10)
        self.fbo.use()
        # Don't shade the tall unused portion of the fixed window buffer, especially
        # when showing the short Gaming strip.
        self.ctx.scissor = (x0, y0, x1 - x0, y1 - y0)
        self.fbo.clear(0, 0, 0, 0)
        self.vao.render(mode=self.ctx.TRIANGLES)
        self.ctx.scissor = None
        d = self.dib
        # Read only the panel and shadow bounds. The DIB is persistent, so the
        # cleared old bounds above remain transparent outside this rectangle.
        rw, rh = x1 - x0, y1 - y0
        needed = rw * rh * 4
        if self._readback.size < needed:
            self._readback = np.empty(needed, dtype=np.uint8)
        self.fbo.read_into(self._readback, viewport=(x0, y0, rw, rh), components=4, alignment=1)
        packed = self._readback[:rw * rh * 4].reshape(rh, rw, 4)
        d.arr[y0:y1, x0:x1] = packed
        t1 = time.perf_counter()
        self.stats["gpu+readback"] = t1 - t0

        if self.dump_path:
            y0, y1 = py - sp, py + h + sp
            region = d.arr[y0:y1].astype(np.float32)
            behind = cap[M - sp:M - sp + (y1 - y0), M - sp:M - sp + self.W, :3].astype(np.float32)
            a = region[..., 3:4] / 255
            comp = region[..., :3] + behind * (1 - a)
            import os
            Image.fromarray(np.clip(comp, 0, 255).astype(np.uint8)[..., ::-1]).save(self.dump_path + ".tmp.png")
            os.replace(self.dump_path + ".tmp.png", self.dump_path)
        blend = BLENDFUNCTION(0, 0, 255, 1)
        user32.UpdateLayeredWindow(hwnd, self.screen_dc, ctypes.byref(wintypes.POINT(win_x, win_y)),
                                   ctypes.byref(SIZE(d.w, d.h)), d.dc, ctypes.byref(wintypes.POINT(0, 0)),
                                   0, ctypes.byref(blend), 2)
        self.stats["update_layered"] = time.perf_counter() - t1
