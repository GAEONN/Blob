# Liquid Glass design system — build brief

Hand this whole file to a new chat. It describes a working Windows design system (the **Blob** app in this repo) precisely enough to build a new
app — a **web browser** — that looks and feels identical.

The reference implementation ships with this repo and is meant to be **reused, not re-derived**:

| File | What it is |
| --- | --- |
| `glass.py` | The whole renderer: screen capture, GPU shader, squircle maths, layered-window output. Reusable as-is. |
| `blob.pyw` | App shell: Win32 window, spring physics, controls, pointer, input, page layout. Copy the patterns. |
| `applemusic.py` | Example of driving another app through UI Automation. |
| `sound.py`, `audio_engine.py`, `media.py`, `engine.py` | Domain code for that app; ignore for a browser. |

Python 3.13 + `moderngl`, `dxcam`, `numpy`, `scipy`, `Pillow`, `pystray`, `psutil`, `uiautomation`.

---

## 1. What the look is

Real glass sitting on the desktop. Not a blurred bitmap: the panel **captures the live screen
behind it and refracts it on the GPU** every frame, so video, games and moving windows bend
through it in real time.

Ingredients, all in one fragment shader (`glass.py` → `FRAG`):

1. **Refraction.** A signed-distance field (SDF) of the shape gives an outward normal `n` and a
   depth `d` inside the edge. Displacement `= n · strength · t^2.2` where
   `t = clamp(1 - d / bevel, 0, 1)`. Panel: `strength 32·S`, `bevel 28·S`. Flat interior gets a
   slight lens: sample position scaled `0.975` about the centre.
2. **Dispersion.** Sample the background three times with displacement × `0.88 / 1.00 / 1.12`
   for B/G/R. Colour splits at every rim, like real glass.
3. **Frost.** 12-tap Poisson blur from a mipmap. Controls are frosted (`frost 1.0`); the panel
   body follows the user's **Glass** slider (`glassiness`, default 0.35).
4. **Specular.** `rim = exp(-d / S)` gives a 1 px edge line. `spec = rim·0.10 + 0.60·max(n·L, 0)
   + 0.26·max(-(n·L), 0)` — lit rim on the light side, a softer glint on the opposite side, plus
   `glow = t³·0.10` inside the bevel. Light vector `L ≈ (-0.55, -0.83)`, nudged by drag velocity.
5. **Adaptive ink.** Text colour is decided **per pixel** from a heavily blurred sample of the
   background (mip level 4.5): `dark = smoothstep(0.50, 0.66, luma)`. Ink is white over dark
   scenery and near-black over bright scenery, so it stays readable when the panel straddles both.
   The glass also leans away from the ink (`push = 0.10 + 0.32·glassiness`) for guaranteed contrast.
6. **Frosted control tint.** On dark scenery controls read as milky white glass. On bright
   scenery the track sinks (grey) and raised parts (selected pill, knobs) become bright frosted
   pills — plus `col -= rim·0.22·dark`, a fine dark edge, so glass is visible on white.
7. **Shadow.** The same SDF offset down by `8·S`, softened with a smoothstep over `[-12·S, 20·S]`
   at 30 % — no separate blur pass.
8. **Ambient wash (optional).** Two colours (top/bottom) mixed over the glass at ~0.24 alpha, e.g.
   pulled from album art. For a browser: the page's theme colour or favicon colour.

**Output path.** Render into an FBO → read back → `UpdateLayeredWindow` with premultiplied BGRA
(per-pixel alpha). Shape edges are pure alpha, so the window is genuinely shaped and clicks fall
through transparent pixels.

---

## 2. Shapes: squircles, done properly

Corners use Apple's continuous curvature, not circular arcs.

* **CPU path** (`squircle_polygon`) is the real [figma-squircle](https://github.com/tienphaw/figma-squircle)
  construction: corner radius + **60 % corner smoothing** (Figma labels this "iOS"). Bézier ramp →
  shortened circular arc → Bézier ramp. Used for masks (album art, list thumbnails, ink fills).
* **GPU path** (`sdShape`) is a superellipse approximation so it can be evaluated per pixel every
  frame: `q = |p - c| - hs + r;  corner = r·(|q.x/r|ⁿ + |q.y/r|ⁿ)^(1/n);  sd = corner + min(max(q.x,
  q.y), 0) - r` with **n = 5** for panels and **n = 2** for capsules/circles (exact).
* **Concentricity.** A shape inside another shape uses `inner radius = outer radius - inset`.
  The mode track (h = 36, r = 18) holds a thumb inset 3 → r = 15.
* Panel radius `34·S`. Artwork `26·S`. List rows `12–14·S`. Pills: `height / 2`.

---

## 3. Motion: springs, not tweens

`Spring(value, k, zeta)`, semi-implicit Euler, sub-stepped at 4 ms (`blob.pyw` → `Spring`).
`zeta < 1` overshoots slightly — that bounce is what makes glass feel liquid.

| Thing | k | zeta | Extra |
| --- | --- | --- | --- |
| Segmented thumb x / width | 330 | 0.62 / 0.70 | stretches with speed: `width ×(1 + min(0.5, \|v\|/1300·S))`, height `×(1 - 0.3·stretch)`; frost drops to 0.25 mid-glide (clearer, more refraction) |
| Toggle knob | 280 | 0.52 | squish `min(0.55, \|v\|/7)`; green tint fades with position |
| Slider knob | 1100 | 0.72 | on grab: radius ×1.5, frost → 0, refraction 9→21·S (a magnifier over the track) |
| Press feedback | 420–500 | 0.55–0.6 | |
| Hover fade | 420 | 1.0 | no overshoot |
| Panel height (page change) | 260 | 0.74 | content crossfades (k 170) while the panel morphs |
| Pointer fusion `k` | 1400 | 0.85 | see below |
| Pull-back of a dragged pill | 700 | 0.62 | |

Rule of thumb from user feedback: **liquid glass, not liquid water.** Short pulls, thick necks,
firm settle. If it looks like it could drip, it's too loose.

---

## 4. The pointer

Over the panel the Windows cursor is hidden (`SetCursor(NULL)`) and replaced by a glass pointer
drawn in the shader. Styles: **Arrow** (rounded dart: tip, two back wings, notch between them —
the default), **Triangle**, **Droplet**, **System** (normal cursor). Height `19·S`, hotspot at the
tip, polygon SDF minus a rounding radius (0.10 of height).

**Surface tension.** When the pointer enters a control it *fuses* with it: the two SDFs are
combined with a smooth minimum, `smin(a, b, k) = min(a,b) - h²·k/4, h = max(k - |a-b|, 0)/k`, with
`k = max(6·S, 1.05·gap + 4·S)`. Inside the control the pointer disappears into it. Moving out
stretches a short thick neck and drags the control's glass toward the pointer
(`offset = 0.05·v + dir·min(gap, 15·S)·0.22`). Past **15 px** the neck snaps, the control springs
back with a wobble, and the pointer floats free. Sliders and toggles don't fuse — the pointer
fades and their knob swells instead.

---

## 5. Layout language

* Panel width 340·S, side padding 22·S. `S = dpi / 96`, everything scales.
* Fonts: **Segoe UI Variable** (`SegUIVar.ttf`) — Display Semibold for big numbers/titles, Text
  Regular 13 for body, 11–12 for captions; **Segoe Fluent Icons** (`SegoeIcons.ttf`) for glyphs;
  **Segoe UI Emoji** fallback for any string containing emoji.
* Text is drawn by Pillow into an **ink coverage layer** (L, 8-bit): 255 main, ~175 secondary,
  ~105 faint, ~45 dividers. Colour comes from the shader (adaptive). Colour accents (warnings) go
  in a separate RGBA layer; images (artwork, favicons) in a third layer that is never recoloured.
* **Tab bar at the bottom**, equal-width segments. The panel is bottom-anchored and grows upward,
  so the tabs never move when pages of different heights swap in.
* Bottom-right of the work area by default; drag anywhere to pin it (it then stays open over
  games and video).

---

## 6. Performance rules (hard-won)

* **Capture with DXGI Desktop Duplication** (`dxcam`), region-limited: ~0.7 ms per changed frame
  and it reports when nothing changed. GDI `BitBlt` was 5 ms every frame — only keep it as the
  fallback path (and for the moment right after the panel moves, when duplication has no new frame).
* **Poll, then draw.** An 8 ms timer checks whether the screen changed; a frame is only rendered
  when the background, an animation or the content actually changed. Idle costs nothing; over
  playing video it reaches 60+ fps at ~4 ms/frame.
* `timeBeginPeriod(1)`, or `WM_TIMER` jitters at 15.6 ms and everything looks choppy.
* **Never block the UI thread.** COM/audio/UI-Automation calls take 50–300 ms; run them on worker
  threads with a job queue and let the UI show optimistic state immediately.
* All shapes are evaluated in the shader from uniform arrays (48 lenses max), so animating them
  costs the CPU nothing. Text layers are re-rendered by Pillow only when the text changes (~4 ms).

## 7. Traps that cost me hours

1. **Self-capture feedback loop.** If the panel captures itself, each frame refracts the previous
   one — colours explode into magenta/cyan, or converge to black. Fix:
   `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)`.
2. That affinity also hides the window from the user's screenshots. Fix: a low-level keyboard hook
   catches **PrtSc** and **Win+Shift+S**, freezes the glass, drops the affinity, waits ~90 ms, then
   replays the keystroke with `SendInput` (inject a dummy key first so a held Win key doesn't open
   Start), and restores afterwards.
3. **Occluded or minimized windows stop rendering**, and then UI Automation can't see their
   content. To drive another app invisibly, make its window transparent + click-through
   (`WS_EX_LAYERED` alpha 1 + `WS_EX_TRANSPARENT`) and keep it on screen — never minimized. Always
   restore the original ex-style in a `finally`.
4. **Distance fields need a padded rasterization**, or a shape touching the bitmap edge gets an
   infinite interior distance and the bevel breaks along straight edges.
5. `np.gradient` needs ≥ 2 px per axis — hairlines (carets, scroll indicators) must be plain rects.
6. Layered-window content is **premultiplied** BGRA; forget that and everything ghosts.

---

## 8. What to build: the browser

Same panel language, but a window you live in.

**Chrome to build**
* Bottom **tab strip** of glass capsules: the active tab is the raised frosted pill that slides and
  stretches between tabs exactly like the segmented control; favicons in the image layer; a close
  "×" that appears on hover; "+" opens a tab with a spring.
* **Address bar** as a glass capsule with the adaptive-ink caret (reuse the search-field pattern in
  `blob.pyw` → `_music_search`), a suggestion list of glass rows that the pointer fuses with.
* **Back / forward / reload** as round glass buttons (`glass_button`), with the same hover fusion.
* A **reading/downloads/history** panel that slides in as a second glass sheet.
* Keep the **Glass slider** and **pointer styles** in Settings — they're per-user taste.

**The one real decision: where the web page is drawn.** A per-pixel-alpha layered window cannot
host a live child HWND, so the page cannot live *inside* the glass window. Pick one:

1. **Glass chrome over a normal content window (recommended).** A plain host window holds a
   WebView2 (`pythonnet` + `Microsoft.Web.WebView2`, or write the shell in C#/WinUI 3). The glass
   panels are separate layered windows positioned over/next to it and moved with it. You get real
   refraction of the page behind the chrome, which is the whole point of the look.
2. **All-in-WebView2.** Draw the chrome in HTML/CSS inside the WebView and fake the glass with
   `backdrop-filter`. Much simpler, but no true refraction, no dispersion, no per-pixel adaptive
   ink — it will look like frosted plastic, not this.
3. **Offscreen composition.** WebView2 visual hosting into a DirectComposition surface you own,
   then refract the page texture yourself. Most faithful (the glass could even bend the page as it
   scrolls), most work, and you'd be sampling a texture instead of the screen.

Start with (1), keep `glass.py` unchanged, and treat each piece of chrome as a panel.

**Definition of done**
* 60 fps over playing video, ≤ 5 ms/frame, ~0 % CPU when idle.
* Text readable on a white page and on a black one, in the same window, at the same time.
* Every control animates with springs; nothing snaps instantly except deliberate state flips.
* Screenshots of the browser work.
* No self-capture artifacts, no leaked window styles, no UI-thread stalls over 16 ms.

**How to verify (same loop I used)**
Set `BLOB_DUMP=<path>.png` to have every rendered frame written out composited over the real
background, and `BLOB_PROFILE=1` to log per-frame timings (capture / GPU+readback /
UpdateLayeredWindow / content redraw). Drive the UI by posting `WM_MOUSEMOVE` / `WM_LBUTTONDOWN` to
the window to capture animation states without touching the mouse.

## Components

### Hardware card and mode bubble

The first view is named **Hardware**, not Blob. Its 340 DIP card keeps CPU/GPU thermals and the first
two fan readings above the fold, followed by a five-part mode track. One context chevron expands a
bounded, scrollable inventory that also includes every discovered fan. One unmarked top-right circle
morphs the view into a 104 × 98 DIP mode bubble. Width, height, content and the signed-distance
silhouette spring together rather than swapping at either endpoint. The shader draws the final outline as the smooth union of a
78 DIP cycle body and a 43 DIP restore satellite; it is not a rounded-rectangle approximation.
Bubble clicks cycle Auto → Quiet → Balanced → Turbo. Custom is intentionally excluded because
it requires the full card. A selected preference must be labeled monitoring-only whenever no real
fan-profile backend reports an active mode.

The top and right edges remain anchored throughout the morph so the card's unmarked corner lens
appears to become the final bubble. Bubble placement is locked by default. The same configurable
overlay shortcut controls Gaming and bubble edit mode; Settings records any modified key chord and
persists it through `RegisterHotKey`.

### Gaming strip and Playing Next extension

This addition describes the native extension in `blob.pyw`, `gaming.py` and
`applemusic.py`; the incumbent brief and design tokens remain unchanged.

**Slim-strip rule.** Gaming rests at 624 × 86 DIP (560 DIP wide in Compact), with
six aligned groups: FPS/frame time, CPU temperature/load, GPU temperature/load,
RAM percentage/used memory, fan RPM and GPU power/power-source hint. The left menu
reveals the existing spring-animated switcher, adding 56 DIP of height. Dragging
and clicking the strip preserve foreground focus. It reuses refractive glass,
continuous corners, adaptive ink and the existing warm temperature warnings.

**Unknown-stays-unknown rule.** Unavailable metrics use dashes or explicit sensor
and setup hints. FPS/frame time describe application presentation intervals, not
refresh rate, display FPS or input latency. Gaming refreshes its text every half
second and stops frame capture when hidden; gameplay validation is not claimed.

**Cached-queue rule.** Playing Next retains its rows while a refresh is pending;
loading, empty and refresh-failure messages remain distinct. The reader targets
Apple Music's queue rather than sidebar playlists. Repeated requests coalesce;
track changes detected on Music force a refresh. Covers arrive independently and
update matching title/artist identities. Rows retain existing hover glass,
ellipsized text and optional covers: 42 DIP regular rows include artist captions,
while 34 DIP Compact rows show titles only. Selecting a row targets its title and
artist in Apple Music rather than relying on a stale queue index.
