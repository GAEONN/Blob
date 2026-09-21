"""Liquid-glass renderer for a Win32 layered window.

Every frame: grab the pixels behind the panel (the window is excluded from capture, so we
never see ourselves), upload them to the GPU, where one fragment shader evaluates every glass
shape analytically and does the refraction, dispersion, frosted blur, lighting and per-pixel
adaptive text colour; the result is read back and pushed with UpdateLayeredWindow (per-pixel
alpha). The CPU only moves bytes, so shapes can animate every frame.

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
MAX_LENSES = 48

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
uniform vec4 ambient;                 // page tint at the top (e.g. the album's colour), alpha = strength
uniform vec3 ambient2;                // page tint at the bottom
uniform vec2 ink0_size, ink1_size;   // in texels (content is rendered supersampled)
uniform float ink_ss;                // texels per window pixel
uniform float fade;                   // 0 = previous content, 1 = current
uniform vec2 bg_size, cap_origin;     // window px + cap_origin = bg px
uniform vec2 panel_pos, panel_size;   // panel rect in window px
uniform float panel_r;
uniform float panel_morph;            // 0 card, 1 contextual bubble; continuous during transition
uniform float bubble_pulse;           // music energy/breath, 0..1
uniform vec2 light;
uniform float S, glassiness;
uniform int n_lens;
uniform vec4 L_rect[48];  // x0 y0 x1 y1 (panel px)
uniform vec4 L_a[48];     // radius, strength, bevel, zoom
uniform vec4 L_b[48];     // rim gain, frost, lift, raised
uniform vec4 L_c[48];     // tint rgb, tint alpha
uniform vec4 L_d[48];     // ink fill, corner exponent, -, -
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

const float SQUIRCLE_N = 2.2;      // fitted against figma-squircle, 60 % corner smoothing
const float SQUIRCLE_K = 1.08;     // radius factor that goes with it

float sdShape(vec2 p, vec2 c, vec2 hs, float r, float n) {
    vec2 q = abs(p - c) - hs + vec2(r);
    vec2 qp = max(q, vec2(0.0)) / max(r, 1e-3);
    float corner = r * pow(pow(qp.x, n) + pow(qp.y, n) + 1e-12, 1.0 / n);
    return corner + min(max(q.x, q.y), 0.0) - r;
}

float sdPanel(vec2 p) {
    vec2 c = panel_size * 0.5;
    float card = sdShape(p, c, c, panel_r * SQUIRCLE_K, SQUIRCLE_N);
    // A smoothly fused 78 px mode body and 43 px restore satellite. The ratios
    // match Panel.BUBBLE_W/H. Relative centres let the two lobes emerge while
    // the card is contracting instead of appearing only after the resize.
    float u = min(panel_size.x / 104.0, panel_size.y / 98.0);
    vec2 main_c = panel_size * vec2(43.0 / 104.0, 55.0 / 98.0);
    vec2 nub_c = panel_size * vec2(79.5 / 104.0, 22.5 / 98.0);
    float a = length(p - main_c) - (39.0 + 2.8 * bubble_pulse) * u;
    float b = length(p - nub_c) - (21.5 + 0.7 * bubble_pulse) * u;
    float k = (9.0 + 1.4 * bubble_pulse) * u;
    float h = max(k - abs(a - b), 0.0) / max(k, 1e-3);
    float bubble = min(a, b) - h * h * k * 0.25;
    float t = smoothstep(0.0, 1.0, clamp(panel_morph, 0.0, 1.0));
    return mix(card, bubble, t);
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
    return q / size;
}
float inkAt(sampler2D t, vec2 size, vec2 pp) {
    vec2 uv = contentUV(size, pp);
    if (uv.x < 0.0 || uv.y < 0.0 || uv.x >= 1.0 || uv.y >= 1.0) return 0.0;
    return texture(t, uv).r;
}
vec4 accAt(sampler2D t, vec2 size, vec2 pp) {
    vec2 uv = contentUV(size, pp);
    if (uv.x < 0.0 || uv.y < 0.0 || uv.x >= 1.0 || uv.y >= 1.0) return vec4(0.0);
    vec4 c = texture(t, uv);
    return vec4(c.rgb * c.a, c.a);      // straight alpha in, premultiplied out
}

void main() {
    vec2 w = gl_FragCoord.xy;
    vec2 pp = w - panel_pos;                         // panel-local px
    vec2 pc = panel_size * 0.5;
    float sdP = sdPanel(pp);
    float mask = clamp(0.5 - sdP / 1.3, 0.0, 1.0);
    float sdS = sdPanel(pp - vec2(0.0, 8.0 * S));
    float shadow = 0.30 * (1.0 - smoothstep(-12.0 * S, 20.0 * S, sdS));
    if (mask <= 0.0) { frag = vec4(0.0, 0.0, 0.0, shadow); return; }

    // ── optics accumulation: the panel itself, then every control lens ──
    vec2 d = vec2(0.0);
    float frost = 0.0, lift = 0.0, raised = 0.0, fill = 0.0;
    float rim = 0.0, face = 0.0, glow = 0.0;
    vec3 tint = vec3(0.0); float tint_a = 0.0;

    vec2 nP = nPanel(pp);
    float depth = max(-sdP, 0.0);
    float t = clamp(1.0 - depth / (28.0 * S), 0.0, 1.0) * clamp(0.5 - sdP, 0.0, 1.0);
    d += nP * (32.0 * S) * pow(t, 2.2);
    d += (pp - pc) * (0.975 - 1.0) * mask;
    float r0 = exp(-depth / S) * clamp(0.9 - sdP, 0.0, 1.0);
    rim += r0; face += r0 * dot(nP, light); glow += pow(t, 3.0) * 0.10;

    for (int i = 0; i < n_lens; i++) {
        vec4 R = L_rect[i];
        if (i != ptr_target && (pp.x < R.x - 1.0 || pp.y < R.y - 1.0 || pp.x > R.z + 1.0 || pp.y > R.w + 1.0))
            continue;
        vec4 A = L_a[i], B = L_b[i], C = L_c[i], D = L_d[i];
        vec2 c = (R.xy + R.zw) * 0.5;
        float sd = lensSd(i, pp);
        float cov = clamp(0.5 - sd, 0.0, 1.0);
        if (cov <= 0.0 && sd > 0.8) continue;
        vec2 g = vec2(lensSd(i, pp + vec2(0.5, 0.0)) - lensSd(i, pp - vec2(0.5, 0.0)),
                      lensSd(i, pp + vec2(0.0, 0.5)) - lensSd(i, pp - vec2(0.0, 0.5)));
        vec2 n = g / max(length(g), 1e-5);
        float dep = max(-sd, 0.0);
        float tt = clamp(1.0 - dep / max(A.z, 1.0), 0.0, 1.0) * clamp(0.5 - sd, 0.0, 1.0);
        d += n * A.y * pow(tt, 2.2);
        d += (pp - c) * (A.w - 1.0) * cov;
        float rr = exp(-dep / S) * clamp(0.9 - sd, 0.0, 1.0) * B.x;
        rim += rr; face += rr * dot(n, light); glow += pow(tt, 3.0) * 0.10 * B.x;
        frost = max(frost, cov * B.y);
        lift = max(lift, cov * B.z);
        raised = max(raised, cov * B.w);
        fill = max(fill, cov * D.x);
        float ta = cov * C.a;
        tint = mix(tint, C.rgb, ta > 0.0 ? ta / max(tint_a + ta, 1e-4) : 0.0);
        tint_a = max(tint_a, ta);
    }

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
    vec3 clear = vec3(at(p + d * 0.88, 0.5).r, at(p + d, 0.5).g, at(p + d * 1.12, 0.5).b);
    float frost_amt = max(frost, glassiness * 0.9);
    vec3 col = clear;
    if (frost_amt > 0.001) {
        float rad = mix(7.0, 3.0 + 10.0 * glassiness, frost > glassiness * 0.9 ? 0.0 : 1.0) * S;
        float lod = 1.5 + 1.5 * glassiness;
        vec3 acc = vec3(0.0);
        for (int k = 0; k < 12; k++) acc += at(p + d + TAPS[k] * rad, lod);
        col = mix(clear, acc / 12.0, frost_amt);
    }
    float l = luma(col);
    col = mix(vec3(l), col, 1.15);

    // text colour follows the scenery behind each spot; the glass leans away from the ink
    float local = luma(at(p, 4.5));
    float dark = smoothstep(0.50, 0.66, local);
    float push = 0.10 + 0.32 * glassiness;
    col = mix(col, vec3(0.0), push * (1.0 - dark));
    col = mix(col, vec3(1.0), (push + 0.08) * dark);
    col = mix(col, mix(ambient.rgb, ambient2, clamp(pp.y / panel_size.y, 0.0, 1.0)), ambient.a);

    // frosted controls: milky on dark scenery; on bright scenery tracks sink, raised parts glow
    vec3 tint_dark_scene = vec3(1.0);
    vec3 tint_bright_scene = mix(vec3(0.0), vec3(1.0), raised);
    float amt = mix(lift, mix(lift * 1.2, 0.55 + lift, raised), dark);
    col = mix(col, mix(tint_dark_scene, tint_bright_scene, dark), clamp(amt, 0.0, 1.0));
    col = mix(col, tint * mix(1.0, 0.85, dark), clamp(tint_a, 0.0, 1.0));

    vec4 pic = mix(accAt(pic0, ink0_size, pp), accAt(pic1, ink1_size, pp), fade);
    col = col * (1.0 - pic.a) + pic.rgb;

    float spec = rim * 0.10 + 0.60 * max(face, 0.0) + 0.26 * max(-face, 0.0);
    spec += glow * clamp(-dot(d, light) / (20.0 * S), 0.0, 1.0);
    col += spec + 0.03;
    col -= rim * 0.22 * dark;

    vec3 ink_col = mix(vec3(1.0), vec3(0.07), dark);
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
                f = cam.grab(region=(sx0 - l, sy0 - t, sx1 - l, sy1 - t))
            except Exception:
                f = None
        if f is not None:
            out[sy0 - y:sy1 - y, sx0 - x:sx1 - x] = f
            return True
        if moved:  # the desktop didn't change but we moved: grab the new spot directly
            gdi_grab(self.screen_dc, x, y, w, h, out)
            return True
        return False


_gdi_dib = None


def gdi_grab(screen_dc, x, y, w, h, out):
    global _gdi_dib
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
        self.M = int(44 * scale)         # extra background grabbed so edges can refract "outside"
        self.w, self.hmax = width, max_height
        self.W, self.H = width + 2 * self.sp, max_height + 2 * self.sp
        self.screen_dc = user32.GetDC(None)
        self.dib = Dib(self.W, self.H)
        self.cap = np.zeros((max_height + 2 * self.M, width + 2 * self.M, 4), np.uint8)
        self.source = ScreenSource()
        self.dump_path = None
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
        self.inks = [None, None]
        self.accs = [None, None]
        self.pics = [None, None]
        units = {"bg": 0, "ink0": 1, "ink1": 2, "acc0": 3, "acc1": 4, "pic0": 5, "pic1": 6}
        for name, unit in units.items():
            self.prog[name].value = unit
        self.prog["bg_size"].value = (self.cap.shape[1], self.cap.shape[0])
        self.prog["S"].value = float(scale)
        self.prog["panel_r"].value = float(34 * scale)
        self.prog["panel_morph"].value = 0.0
        self.prog["bubble_pulse"].value = 0.0
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
        return self.W - self.sp - int(round(w))   # right-aligned, so a narrower panel stays in the corner

    def _textures(self, ink, accent, pic):
        """Layers go up as plain bytes — no float maths, no premultiply on the CPU."""
        ink = np.ascontiguousarray(np.asarray(ink, np.uint8))
        h, w = ink.shape
        out = [ink]
        for layer in (accent, pic):
            a = np.zeros((h, w, 4), np.uint8) if layer is None else np.ascontiguousarray(
                np.asarray(layer, np.uint8))
            out.append(a)
        return out

    def set_content(self, ink, accent, pic=None, instant=False):
        """New content (text, accents, images). The previous one is kept for crossfading."""
        ink, acc, pic = self._textures(ink, accent, pic)
        h, w = ink.shape
        new = []
        for data, comps in ((ink, 1), (acc, 4), (pic, 4)):
            t = self.ctx.texture((w, h), comps, data.tobytes(), alignment=1)
            t.filter = (self.ctx.LINEAR, self.ctx.LINEAR)
            new.append(t)
        layers = [self.inks, self.accs, self.pics]
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

    def replace_content(self, ink, accent, pic=None):
        """Refresh the current content in place (live numbers) without a crossfade."""
        ink_a, acc_a, pic_a = self._textures(ink, accent, pic)
        h, w = ink_a.shape[:2]
        if self.inks[1] is None or self.inks[1].size != (w, h):
            return self.set_content(ink, accent, pic, instant=True)
        self.inks[1].write(ink_a.tobytes())
        self.accs[1].write(acc_a.tobytes())
        self.pics[1].write(pic_a.tobytes())
        self._last_key = None

    def set_panel_shape(self, morph):
        self.prog["panel_morph"].value = float(max(0.0, min(1.0, morph)))

    def set_bubble_pulse(self, pulse):
        self.prog["bubble_pulse"].value = float(max(0.0, min(1.0, pulse)))

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
            arr[4, i] = (L.get("fill", 0), L.get("n", 2.0), 0, 0)
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
        sp, M = self.sp, self.M
        h = int(round(panel_h))
        py = self.panel_y(h) if panel_y is None else int(round(panel_y))
        ch, cw = h + 2 * M, self.cap.shape[1]
        self.stats = getattr(self, "stats", {})
        tb = time.perf_counter()
        key = (win_x, win_y, h, int(round(panel_w)))
        moved = key != self._last_key
        changed = self.source.grab(win_x + sp - M, win_y + py - M, cw, ch, self.cap, moved)
        self._last_key = key
        self.stats["capture"] = time.perf_counter() - tb
        return force or moved or changed

    def render(self, hwnd, win_x, win_y, panel_w, panel_h, fade, light, glassiness, panel_y=None):
        S, sp, M = self.S, self.sp, self.M
        h, pw = int(round(panel_h)), int(round(panel_w))
        py = self.panel_y(h) if panel_y is None else int(round(panel_y))
        px = self.panel_x(pw)
        cap = self.cap
        ch, cw = h + 2 * M, cap.shape[1]
        t0 = time.perf_counter()
        self.bg.write(cap[:ch], viewport=(0, 0, cw, ch))
        self.bg.build_mipmaps()
        self.bg.use(0)
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
        self.fbo.use()
        self.ctx.scissor = None
        self.fbo.clear(0, 0, 0, 0)
        # Don't shade the tall unused portion of the fixed window buffer, especially
        # when showing the short Gaming strip.
        self.ctx.scissor = (max(0, px - sp), max(0, py - sp), min(self.W, pw + 2 * sp), min(self.H, h + 2 * sp))
        self.vao.render(mode=self.ctx.TRIANGLES)
        self.ctx.scissor = None
        d = self.dib
        self.fbo.read_into(d.arr, components=4, alignment=1)
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
