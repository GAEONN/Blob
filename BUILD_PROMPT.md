# Build prompt — paste this into a fresh AI coding agent

You are building **Blob**, a Windows desktop app. Work on a Windows 11 machine with Python 3.10+.
Build it incrementally, run it after every stage, and verify with real output — never assume a
stage works because the code looks right.

---

## What Blob is

A tray app that shows the CPU temperature as a number in the notification area. Clicking it opens
a small pane made of **liquid glass**: it captures the live screen behind itself and refracts it on
the GPU, so video and games bend through it in real time. The pane has four views, switched by a
bar at the bottom:

1. **Blob** — vendor-neutral, read-only CPU/GPU, memory, storage and fan monitoring.
2. **Sound** — a system-wide audio enhancer: boost, EQ, bass, clarity, surround, with a spectrum.
3. **Music** — a real Apple Music player: now playing, catalog search, queue, playlists.
4. **Settings** — grouped preferences.

Everything must run without administrator rights.

---

## Stack

`moderngl` (OpenGL 3.3), `dxcam`, `numpy`, `scipy`, `Pillow`, `pystray`, `psutil`, `sounddevice`,
`pycaw`, `comtypes`, `uiautomation`, `winrt-Windows.Media.Control`. No Electron, no browser, no UI
framework: the window is a raw Win32 layered window and every pixel is drawn by one fragment shader.

---

## Stage 1 — The glass renderer (do this first, it is the whole product)

A borderless `WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST` popup window, updated with
`UpdateLayeredWindow` from a premultiplied BGRA DIB section. Per frame:

1. **Capture** the screen behind the pane with DXGI Desktop Duplication (`dxcam`), region-limited to
   the pane plus a ~44 px margin. Keep GDI `BitBlt` as a fallback, and use it for the one frame after
   the pane moves (duplication reports no new frame then). Duplication tells you when nothing
   changed — skip the frame entirely.
2. **Upload** that to a texture, build mipmaps.
3. **One fragment shader** does everything:
   - **Shape** as a signed distance field. Corners are squircles: a superellipse with
     **n = 2.2 and the radius scaled 1.08** — that is a numerical fit to figma-squircle at 60 %
     corner smoothing, which is what Apple uses. (A plain `n = 5` superellipse is visibly squarer;
     I measured ~780 px of area error versus ~29.)
   - **Refraction**: outward normal `n` from the SDF, depth `d` inside the edge,
     `t = clamp(1 - d/bevel, 0, 1)`, displacement `= n · strength · t^2.2`, with `strength ≈ 32·S`
     and `bevel ≈ 28·S`. The flat middle gets a slight lens: sample position scaled 0.975 about the
     centre.
   - **Dispersion**: sample the background three times at 0.88 / 1.00 / 1.12 of the displacement for
     B/G/R.
   - **Frost**: a 12-tap Poisson blur from a mipmap. Controls are always frosted; the pane body
     follows a user "Glass" slider.
   - **Specular**: `rim = exp(-d/S)` gives a 1 px edge line;
     `spec = rim·0.10 + 0.60·max(n·L,0) + 0.26·max(-(n·L),0)`, plus a soft inner glow `t³·0.10`.
   - **Adaptive ink**: sample the background at a high mip level, `dark = smoothstep(0.50, 0.66,
     luma)`, and colour text per pixel — light over dark scenery, near-black over bright. Also push
     the glass away from the ink for guaranteed contrast. This is what makes it readable over a
     white page and a black one at the same time.
   - **Shadow**: the same SDF offset down 8 px, smoothstepped over ~[-12, +20] px at 30 %.
   - Never cut the rim or bevel with a hard `step()`; fade across a pixel or the curves stair-step.
4. **Read back** the framebuffer straight into the DIB and push it.

**Critical:** call `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)`. Without it the pane
captures its own previous frame and the image explodes into magenta, then black. The side effect is
that the pane is invisible to screenshots, so install a low-level keyboard hook: on PrtSc or
Win+Shift+S, freeze the glass, drop the affinity, wait ~90 ms, replay the keystroke with
`SendInput`, and restore afterwards. (Inject a dummy key first so a held Win key doesn't open Start.)

**Content layers.** Text and artwork are drawn with Pillow into three layers uploaded as textures:
an **ink coverage** layer (8-bit; the shader colours it), an **accent** layer (RGBA, for warning
colours) and an **image** layer (artwork, never recoloured). Draw them at **2× and let the GPU
filter down** — it is the difference between crisp and pixelated. Upload straight bytes and
premultiply in the shader; doing it in numpy costs ~40 ms per redraw. Anchor the layers to the
**bottom** of the pane so the tab bar never moves while a page morphs to a new height.

**Performance targets:** ≤ 5 ms per frame, 60 fps over playing video, ~0 % CPU when the screen is
static, ≤ 10 ms to redraw the text layers. Poll for screen changes on an 8 ms timer but only draw
when something changed. Call `timeBeginPeriod(1)` or `WM_TIMER` jitters at 15.6 ms and everything
looks choppy.

## Stage 2 — Motion

Everything animates with **springs**, never tweens: `Spring(value, k, zeta)`, semi-implicit Euler,
sub-stepped at 4 ms. `zeta < 1` overshoots slightly, and that bounce is the whole feel.

| Element | k | zeta | Behaviour |
| --- | --- | --- | --- |
| Selected pill (tabs, modes) | 330 | 0.62 | slides and stretches with speed; frost drops mid-glide so it looks clearer while moving |
| Toggle knob | 280 | 0.52 | squishes, colour fades with travel |
| Slider knob | 1100 | 0.72 | on grab: 1.5× radius, frost → 0, refraction up — a magnifier over the track |
| Page height / crossfade | 260 / 170 | 0.74 / 1.0 | the pane morphs while content crossfades |
| Pointer fusion | 1400 | 0.85 | see below |

Compute every shape in the shader from uniform arrays (allow ~48 "lenses"), so animating costs the
CPU nothing.

**The pointer.** Over the pane, hide the Windows cursor and draw your own glass pointer (a rounded
dart; offer Arrow / Triangle / Droplet / System in settings). When it enters a control the two SDFs
merge with a smooth minimum — `smin(a,b,k) = min(a,b) - h²k/4, h = max(k-|a-b|,0)/k` — so the
pointer melts into it. Moving away stretches a short thick neck and drags the control's glass along;
past ~15 px the neck snaps and the control springs back. Keep it **thick**: it is liquid glass, not
water. If it looks like it could drip, the springs are too loose.

## Stage 3 — Vendor-neutral hardware monitoring

* Use Windows performance counters for CPU/GPU load and clocks, psutil for memory/battery, and
  storage IOCTLs for NVMe temperatures. Monitoring is read-only; do not ship OEM fan/power control.
* **NVIDIA**: NVML (`nvml.dll`) for temperature, load, power, clocks. Only call it while the GPU is
  awake — check the PnP power state through `cfgmgr32` first, or you keep the dGPU from sleeping and
  drain the battery.
* Read **LibreHardwareMonitor** over WMI (`root\LibreHardwareMonitor`) or
  **HWiNFO** from its shared memory (`Global\HWiNFO_SENS_SM2`: header, then fixed-size reading
  structs; values are 8-byte aligned — mind the padding). CPUID HWMonitor exposes nothing. Last
  resort: the `\Thermal Zone Information(*)\Temperature` performance counter, which always works.
* Where Windows cannot expose something, **say what optional provider enables it**. Never show a
  dead button or a row of dashes.

## Stage 4 — Sound

Windows plays into the **VB-Audio Virtual Cable**; a **separate process** reads it back from "CABLE
Output", processes it, and plays it on the real device. Chain: 10-band EQ → bass shelf → clarity
shelf → stereo width → loudness compressor → look-ahead limiter (ceiling −0.5 dBFS, one block of
look-ahead). Biquads via `scipy.signal.sosfilt` with carried state; keep the section count fixed so
state survives a settings change.

Two things make or break this:
* **Order matters.** Compress *before* the boost: quiet parts come up, loud parts come down, then
  everything is lifted. Compressing after the boost cancels it (I measured +2 dB instead of +12 dB).
* **Volume hand-off.** Set the real output device to 100 % and move the user's volume slider onto
  the cable (this is what FxSound does). Otherwise the system volume silently eats ~10 dB of the
  boost.

Run the DSP in its own process so the UI can never stutter the audio, exchange parameters and the
spectrum through shared memory, and have the engine restore the user's default device if the UI
dies. Start the engine warm at launch so the on/off switch is instant (~140 ms, not seconds), and do
every COM/device call on a worker thread.

## Stage 5 — Music

* **Now playing** from Windows' media session API (`GlobalSystemMediaTransportControlsSessionManager`
  via winrt): title, artist, artwork, position, play/pause/next/previous/seek. Works with any player.
* **Apple Music, properly.** There is no API for the Windows app, so drive the app itself:
  * **Search** Apple's public catalog: `https://itunes.apple.com/search?term=…&entity=song` — no login.
  * **Play** a song: open its `musics://music.apple.com/…` link, wait for the track's row in the app's
    accessibility tree, open that row's "More" menu and invoke its **Play "…"** entry.
  * **Playlists** come from the app's sidebar; **Playing Next** from its queue panel; shuffle and
    repeat are the app's own transport buttons.
  * The app only builds its page while it is on screen, so make its window **transparent and
    click-through** (`WS_EX_LAYERED` alpha 1 + `WS_EX_TRANSPARENT`) during the action instead of
    minimising it — the user never sees it, and a song starts in ~3 s instead of ~6. Preload the page
    when the pointer rests on a result and a click starts playback in ~1.4 s. Always restore the
    window's original styles in a `finally`, and press its "Try Again" button if it shows an error.

## Stage 6 — The pane itself

* **Layout**: page-specific forms anchored bottom-right and draggable to pin anywhere.
  Fonts: Segoe UI Variable (Display Semibold for numbers, Text for body), Segoe Fluent Icons for
  glyphs, Segoe UI Emoji as fallback — playlist names contain emoji.
* **Nested corners follow one rule**: `outer radius = inner radius + padding`. Pane 34, artwork inset
  22 → 12, and so on.
* Avoid a universal Regular/Compact switch. Music rests as a 340 px Apple Music-style mini player;
  hovering its cover reveals an expand affordance. Expanded artwork reveals its metadata, timeline,
  transport and collapse control only while hovered.
* Music also owns an unmarked contextual corner circle. Keep its footprint clear of metadata and the
  seek target, then spring the mini player continuously into an audio-reactive fused bubble anchored
  at that corner. The main lobe uses tap for play/pause, double-tap for next and hold for previous;
  the attached satellite restores the mini player. Add the gesture legend to Music settings rather
  than labelling the bubble itself. Bubble dragging follows the global overlay lock state.
* In Music card and cover modes, flank the default previous/play-next row with Volume and Options.
  Volume swaps the middle row for the system-volume slider; Options swaps it for Search, Playing
  Next, a persisted reactive-bubble equalizer toggle, Shuffle and Repeat. Keep the two edge anchors
  visible while either inline utility is open. When enabled, map spectrum bass/mids/treble to the
  main lobe, fused neck and satellite through separate bounded springs. Use the default Windows
  endpoint peak as a truthful amplitude fallback when the optional DSP spectrum is unavailable.
* Hardware owns a contextual 104 × 98 mode bubble instead of inheriting the universal compact
  layout. Its card affordance is one unmarked glass circle. The card's width, height, crossfaded
  content and signed-distance silhouette must spring continuously into the bubble, which is the
  smooth union of a large cycle target and a smaller restore satellite.
  Preserve the card's top-right anchor during that morph. Bubble placement follows the shared
  overlay lock state. Settings must include a
  click-to-record modified-key editor for that binding.
  Its body cycles Auto → Quiet → Balanced → Turbo; Custom stays on the full card. The card
  exposes a five-mode selector and a single chevron for the bounded, scrollable sensor inventory.
* **The view switcher rests as a small pill** with the current view's name and springs open into the
  five options when the pointer reaches it.
* **Lock is global and explicit.** Blob starts unlocked. Entering Gaming, switching views and
  hide/show cycles must preserve the current state. Only the configured bind or tray action toggles
  it; locked makes the entire overlay click-through and unlocked restores controls and dragging.
* **Settings** grouped by area (Appearance, Music, Sound, Hardware, System), scrollable with the wheel,
  with a switch for every customization, including one that makes the pane visible to screen
  recorders (which necessarily freezes the glass).
* Transport marks (play, pause, skip, shuffle, repeat) are drawn as **solid shapes**, not font glyphs.

---

## How to verify (do not skip)

You cannot see the screen, so build the instrumentation first:

* An environment variable that writes every rendered frame to a PNG, composited over the real
  background, so you can look at exactly what the user sees.
* Another that logs per-frame timings: capture, GPU + readback, UpdateLayeredWindow, text redraw.
* Drive the UI by posting `WM_MOUSEMOVE` / `WM_LBUTTONDOWN` / `WM_MOUSEWHEEL` to the window to
  capture hover, press and mid-animation states without touching the mouse.
* Test the audio chain offline (feed a synthetic signal through the DSP and measure loudness and
  peak) before testing it live, and test binary parsers against a synthetic block you build yourself.
* Simulate Intel-, AMD- and NVIDIA-only hardware paths and missing optional providers.

## Traps that will cost you hours

1. The pane refracting its own previous frame (see Stage 1).
2. Occluded or minimized windows stop rendering, and their accessibility tree empties — this breaks
   both the glass (no, it doesn't) and any app automation (yes, it does).
3. Distance fields need a padded rasterization, or a shape touching the bitmap edge reports infinite
   interior distance and the bevel breaks along straight edges.
4. `np.gradient` needs ≥ 2 px per axis: hairlines (carets, scrollbars) must be plain rectangles.
5. Layered-window content is premultiplied BGRA; forget it and everything ghosts.
6. COM, audio and accessibility calls take 50–300 ms. On the UI thread they cause visible stutter
   every time they run. Queue them on workers and show optimistic state.
7. A global rename ("thermals" → "blob") will also rename your functions. Check afterwards.

## Definition of done

Frames under 5 ms over playing video; idle near 0 % CPU; text readable over white and black at once;
every control animates; screenshots work; the boost measurably louder (~+12 dB on already-loud
material, peaks still capped); a song starts in about a second when preloaded; and on a machine with
none of the supported hardware, every page still opens and explains what it cannot do.
