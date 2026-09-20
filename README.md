# Blob

A liquid-glass control panel for Windows laptops. One small pane, drawn entirely by a GPU shader,
that refracts the live desktop behind it — video, games, anything — and holds three things:

* **Thermals** — CPU and GPU temperatures, fan speeds, and automatic fan-profile switching on ASUS laptops.
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

**Thermals.** CPU temperature and fan RPM come from the ASUS ATKACPI interface (the one Armoury
Crate uses), the GPU from NVML, the SSD from a storage IOCTL, the motherboard from a Windows
performance counter. No admin rights, no kernel driver. *Auto* mode watches load and temperature
and switches between the laptop's Silent / Balanced / Turbo profiles with hysteresis, so it ramps up
quickly and calms down slowly. The tray icon shows the CPU temperature as a number.

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

**Regular** is the full pane. **Compact** (Settings → Size) is a narrow version for a screen
corner while you play: temperatures and fan mode at a glance, a mini music player with artwork and
transport, boost and the spectrum. Clicking the album art opens it as artwork only, Apple Music
style; clicking it again goes back.

Settings also holds **Show in screen recordings**. Off (the default) keeps the pane invisible to
capture so the glass can refract live. On makes it visible to recorders, at the cost of freezing
the glass to the backdrop captured when it appeared, since it would otherwise refract itself.

## On other machines

Blob runs on any Windows 11 laptop; the parts that depend on hardware degrade instead of breaking.

| Part | ASUS + NVIDIA | Anything else |
| --- | --- | --- |
| CPU temperature | ASUS ATKACPI | LibreHardwareMonitor or HWiNFO if one is running, otherwise the ACPI thermal zone |
| Fan speeds | ATKACPI | LibreHardwareMonitor or HWiNFO, otherwise hidden |
| Fan modes | Silent / Balanced / Turbo | hidden, with a line saying why |
| GPU | NVML (temp, load, power) | NVML on any NVIDIA card; otherwise a hardware monitor for the temperature |
| Sound, Music, glass | full | full |

On an Intel or AMD CPU, run one of these in the background and Blob picks its sensors up by itself:

* [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) — read over WMI, nothing to configure.
* [HWiNFO](https://www.hwinfo.com/) — read from its shared memory; switch on *Settings → Shared Memory Support*.

CPUID **HWMonitor** is not usable: it shows sensors but offers nothing for other programs to read.
The Settings page names whichever source Blob ended up using. The Sound page needs
[VB-Audio Virtual Cable](https://vb-audio.com/Cable/) and says so if it's missing.

## Requirements

* Windows 11, a GPU with OpenGL 3.3
* Python 3.13
* For fan control: an ASUS laptop with the ATKACPI driver (ships with ASUS System Control Interface)
* For the sound page: [VB-Audio Virtual Cable](https://vb-audio.com/Cable/)
* For the music page: the Apple Music app from the Microsoft Store

```
pip install -r requirements.txt
python make_shortcut.py    # builds the icon and a desktop shortcut
```

Then run `blob.pyw` (or use the shortcut). It lives in the tray; click the temperature to open the
pane, drag the pane to pin it anywhere, press Esc to dismiss it.

## Layout

| File | Role |
| --- | --- |
| `glass.py` | The renderer: capture, shader, squircle geometry, layered-window output |
| `blob.pyw` | App shell: window, springs, controls, pointer, pages, input |
| `engine.py` | Sensors and the fan-profile controller |
| `sound.py` / `audio_engine.py` | Audio routing and the DSP process |
| `media.py` | Windows now-playing |
| `applemusic.py` | Apple Music search and playback |

## Notes

To refract the live screen the window has to exclude itself from screen capture, or it would
refract its own previous frame. A keyboard hook makes screenshots (PrtSc, Win+Shift+S) work anyway
by briefly dropping that exclusion. Screen recorders still won't see the pane.

Written for one laptop (an ASUS TUF A14) and generalised where it was cheap to do so. Sensor IDs,
fan profiles and the Apple Music automation are the parts most likely to need adjusting elsewhere.

## Gaming strip and Playing Next

Choose **Gaming** in Blob's view switcher for a draggable, always-on-top glass strip:
FPS, frame time, CPU/GPU temperatures and utilization, RAM, fan RPM, and GPU power.
Gaming opens **locked and click-through**: mouse clicks go to the game, not Blob.
Press **Ctrl+Alt+G** to unlock/relock, or right-click Blob's tray icon and choose
**Unlock gaming strip / Lock gaming strip** (also available if another app owns the shortcut).
Unlocking expands navigation; drag a gap to reposition, then lock before playing.
Reopening Gaming locks it again. The overlay never requests foreground focus in Gaming.
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
need the setup step. No service, game injection, or privilege change is performed.
Some accounts need [Windows Performance Log Users permission](https://github.com/GameTechDev/PresentMon#user-access-denied);
Blob reports the problem instead of silently elevating. Frame capture stops when
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
