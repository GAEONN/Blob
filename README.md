# Blob

A liquid-glass control panel for Windows laptops. One small pane, drawn entirely by a GPU shader,
that refracts the live desktop behind it — video, games, anything — and holds three things:

* **Hardware** — CPU/GPU load, memory, battery, storage temperatures and optional read-only temperature/fan sensors on Windows PCs.
* **Sound** — a system-wide audio enhancer: boost, EQ, bass, clarity and surround, with a glass spectrum.
* **Music** — a real Apple Music player: now playing, catalog search, your playlists.

No browser, no Electron. Python, OpenGL, and a layered Win32 window.

## Why it looks like that

The pane is not a blurred screenshot. Every frame it captures the screen behind itself with DXGI
Desktop Duplication and refracts it in a single fragment shader: edge displacement from a signed
distance field, colour dispersion, frosted blur, specular rim, and a shadow — all analytic, so any
shape can move or morph for free.

Two details do most of the work:

* **Squircles, not rounded rectangles.** Corners use Apple's continuous curvature (the
  figma-squircle construction at 60 % smoothing on the CPU, a superellipse in the shader), and
  nested shapes follow `inner radius = outer radius − inset`.
* **Per-pixel adaptive ink.** Text colour is decided pixel by pixel from a blurred read of the
  scenery behind it, so one pane can sit half over a white page and half over a black one and stay
  readable in both halves.

Controls are springs, not tweens: the selected pill slides and stretches, knobs swell when grabbed,
pages morph to their new height while the content crossfades. The mouse pointer becomes a glass
dart that fuses into whatever it hovers, stretches a neck when pulled away, and snaps free.

[`DESIGN.md`](DESIGN.md) is the full specification — the shader maths, every spring constant, and
the traps involved in building this kind of window.

## What each page does

**Hardware.** Load, memory, battery, adapters, ACPI thermal zones and NVMe temperatures come from
Windows. CPU/GPU temperatures and fan RPM have no vendor-neutral Windows API, so Blob reads them
from LibreHardwareMonitor/OpenHardwareMonitor or HWiNFO when one is running. NVIDIA NVML adds
power and clock data where available. Monitoring is read-only: Blob does not change firmware fan
curves or power profiles. The Hardware card offers Auto, Quiet, Balanced, Turbo and Custom
preferences while clearly marking them as monitoring-only until a fan-profile backend is connected.
Its single-circle corner control smoothly contracts and reshapes the card into a fused glass mode
bubble: clicking the body cycles the four quick modes, while the attached satellite restores the
full card. The card's top-right remains fixed during the morph so the circle feels like the bubble's
physical origin. Bubble placement follows Blob's shared lock state; while Blob is unlocked it can
be dragged immediately. Expanding the card's chevron
shows every discovered fan RPM plus CPU/GPU load, clocks and power, storage, memory and battery.
The tray icon shows the CPU temperature when one is available.

**Sound.** Windows plays into a virtual cable; a separate process reads it back, runs EQ → bass →
clarity → stereo width → loudness compressor → look-ahead limiter, and plays the result on the real
output device, which is held at 100 % while your volume slider moves to the cable (the same trick
FxSound uses). Measured about +12 dB of perceived loudness at full boost on already-loud material,
with peaks still capped at −0.5 dBFS. The audio runs in its own process so the interface can never
stutter it.

**Music.** Now-playing comes from Windows' media session API, so it also works with Spotify or a
browser tab. Apple Music gets direct control: catalog search through Apple's public search endpoint,
and playback by driving the real Apple Music app through UI Automation while its window is kept
invisible. Resting the pointer on a result preloads it, which brings a click down to about 1.4 s.

## Sizes and the pointer

Each page now chooses the form that fits its job instead of exposing a universal C/R switch. Music
opens as an Apple Music-inspired horizontal mini player. Hovering its cover reveals the expand
affordance; selecting it grows into a cover-first view. Hover the expanded cover to reveal metadata,
timeline, transport and the collapse control that returns to the mini player. Both forms keep a
Volume button at the left of transport and an Options button at the right. Volume replaces the
middle row with an inline system-volume slider; Options exposes Search, Playing Next, Shuffle and
Repeat without crowding the default transport.

The mini player's unmarked corner circle morphs the card into an audio-reactive player bubble:
bass expands the main body, mids flex the liquid neck, and treble animates the restore satellite.
The response is spring-smoothed and bounded so the bubble never escapes its stable hit targets.
The equalizer control in Music Options—or **Reactive music bubble** in Settings—toggles it. A
lightweight Windows endpoint meter keeps amplitude response working when Blob's optional Sound
enhancement is off; the routed Sound spectrum provides true frequency-band detail when available.
The bubble is
anchored to the card's top-right corner. Tap the large lobe to play or pause, double-tap it for the
next track, or hold it for the previous track. Select the small attached lobe to restore the card.
These controls use the active Windows media session, so they are not tied to Apple Music. The same
editable overlay shortcut controls one shared lock state for every tab. While unlocked, either
bubble can be dragged; lock Blob to pass pointer input through the overlay.

Settings also holds **Include Blob in screenshots and recordings**. Off (the default) keeps the
pane invisible to capture so the glass can refract live. On makes it visible to capture tools, at
the cost of freezing the glass to the backdrop captured when it appeared, since it would otherwise
refract itself.

## Hardware compatibility

Blob is designed for Windows 11 PCs, not a particular laptop brand. Core load, memory, battery,
storage, Sound, Music and glass features work without an OEM utility. Optional sensor coverage is:

| Data | Source |
| --- | --- |
| CPU/GPU load | Windows performance counters on Intel, AMD and NVIDIA systems |
| CPU/GPU temperature and fan RPM | LibreHardwareMonitor/OpenHardwareMonitor or HWiNFO shared memory |
| NVIDIA temperature, power and clock | NVML, when available |
| NVMe temperatures | Windows storage IOCTL |

The full installer provisions LibreHardwareMonitor as Blob's sensor provider and starts it minimized
through an elevated logon task, so it can read supported sensors without showing a UAC prompt on
every launch. Blob also detects these providers when the user already runs one:

* [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) — read over WMI, nothing to configure.
* [HWiNFO](https://www.hwinfo.com/) — read from its shared memory; switch on *Settings → Shared Memory Support*.

CPUID **HWMonitor** is not usable: it shows sensors but offers nothing for other programs to read.
The Settings page names whichever source Blob ended up using.

## Requirements

* Windows 11, a GPU with OpenGL 3.3
* Python 3.10–3.13 (the installer creates an isolated environment)
* Administrator approval for the optional sensor and audio drivers

Fresh install or update, including Python when needed, dependencies, the FPS helper,
LibreHardwareMonitor/PawnIO sensors, VB-CABLE audio, desktop shortcut, verification and launch:

```powershell
irm https://raw.githubusercontent.com/alonsoglunac-debug/Blob/main/install.ps1 | iex
```

From an existing checkout:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Or double-click **Install Blob.cmd** in the checkout.

The installer uses one elevated system phase. LibreHardwareMonitor and PresentMon are downloaded
from their official releases and SHA-256 verified. For audio, Blob downloads and verifies the
official VB-Audio package, then shows its original signed setup window; select **Install Driver**.
That elevated phase also adds the installing account to Windows' built-in Performance Log Users
group (resolved by SID, so localized Windows editions work). Sign out and back in once after the
first installation so Windows issues a login token containing the new FPS permission.
VB-Audio requires a Windows restart before Sound is ready. Music does not install or require Apple
Music: Spotify, YouTube, browsers and other Windows media sessions work normally. Apple Music only
adds its optional catalog search and Playing Next integrations when already installed.

For a deliberately partial installation, the local script accepts `-SkipAudio`, `-SkipSensors`,
`-SkipPresentMon`, and `-NoLaunch`. Re-running the installer is safe and repairs missing components.

Then run `blob.pyw` (or use the shortcut). It lives in the tray; click the temperature to open the
pane, drag the pane to pin it anywhere, press Esc to dismiss it.

## Layout

| File | Role |
| --- | --- |
| `glass.py` | The renderer: capture, shader, squircle geometry, layered-window output |
| `blob.pyw` | App shell: window, springs, controls, pointer, pages, input |
| `engine.py` | Vendor-neutral Windows hardware monitoring |
| `sound.py` / `audio_engine.py` | Audio routing and the DSP process |
| `media.py` | Windows now-playing |
| `applemusic.py` | Apple Music search and playback |

## Notes

To refract the live screen the window has to exclude itself from screen capture, or it would
refract its own previous frame. A keyboard hook makes screenshots (PrtSc, Win+Shift+S) work anyway
by briefly dropping that exclusion. Screen recorders still won't see the pane.

Exact sensor availability varies because Windows has no standard temperature/fan API. Blob keeps
the rest of the app usable while sensors are discovered and names the optional provider in Settings.

## Gaming strip and Playing Next

Choose **Gaming** in Blob's view switcher for a draggable, always-on-top glass strip:
FPS, frame time, CPU/GPU temperatures and utilization, RAM, fan RPM, and GPU power.
Blob starts **unlocked** and keeps that state when changing tabs, including Gaming. Press
**Ctrl+Alt+G** to lock/unlock the whole overlay, or use the tray's
**Unlock overlay / Lock overlay** command. Change this binding under Settings → Gaming by clicking
the shortcut and pressing a new modified key combination.
When locked, mouse clicks pass through Blob on every tab. In Gaming, the shortcut modifiers can
still be held to drag temporarily without changing the global lock state. Unlocking Gaming expands
navigation; drag a gap to reposition, then lock before playing. Hiding, reopening, or changing tabs
never changes the state—the bind is authoritative. Gaming never requests foreground focus.
This uses Windows' [layered-window input transparency](https://learn.microsoft.com/en-us/windows/win32/winmsg/window-features#layered-windows),
not just a no-activation mouse handler. Use windowed or borderless
games; an ordinary desktop overlay cannot promise visibility in exclusive fullscreen.
When a focused game moves above Blob in the topmost window group, Gaming restores
its position without activation (checked once per second, not every rendered frame).
For Fortnite, use **Settings > Video > Display > Window Mode > Windowed Fullscreen**
if Fullscreen hides desktop overlays. Blob does not inject into the game or modify
anti-cheat or the game's display settings.

FPS/frame time use actual application presentation intervals from the foreground
process, averaged over a rolling second. They are not monitor refresh rate, display
FPS, input latency, or frame-generation-inclusive FPS. When Blob has focus it retains
the last external foreground process. Missing/stale frame data is shown as a dash.
Hardware measurements retain Blob's existing sensor support and limitations.

The optional FPS helper is the official [PresentMon console application](https://github.com/GameTechDev/PresentMon/blob/main/README-ConsoleApplication.md).
Install the pinned, SHA-256-verified portable binary without an installer:

```powershell
powershell -ExecutionPolicy Bypass -File tools/setup_presentmon.ps1
```

Alternatively set `BLOB_PRESENTMON` to a compatible PresentMon console executable.
This checkout's helper is installed locally but excluded from Git; other installations
need the setup step. No service or game injection is used. The full installer provisions
[Windows Performance Log Users permission](https://github.com/GameTechDev/PresentMon#user-access-denied)
automatically. If Gaming still reports setup immediately afterward, sign out and back in once;
Windows does not add new group membership to processes in the existing login session. Frame capture stops when
Gaming is hidden or closed. Gaming limits glass refresh to approximately 60 Hz,
updates frame statistics twice a second, and avoids shading the unused window area.

Playing Next preserves its cached rows during refreshes, refreshes after track changes,
coalesces repeated requests, and loads artwork separately. Its reader targets Apple
Music's actual queue, excluding sidebar playlists. The initial read still depends on
Apple Music's UI responsiveness; covers also depend on the catalog/network.

## Development checks

Run offline regression tests on Windows with the dependencies installed:

```powershell
python -m unittest discover -s tests -v
```

These check regular/compact layouts at 100–200% scaling, settings and sensor-list scrolling,
player hit targets, playback timing, mask coverage, and capture-buffer reuse. They do not
change audio routing or poll hardware. To render a contact sheet using the real GPU glass
shader over a synthetic background:

```powershell
python tests/render_preview.py --output audit.tmp.png
```

## License

MIT — see [LICENSE](LICENSE).
