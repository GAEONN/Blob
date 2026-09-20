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
| CPU temperature | ASUS ATKACPI | LibreHardwareMonitor if it's running, otherwise the ACPI thermal zone |
| Fan speeds | ATKACPI | LibreHardwareMonitor, otherwise hidden |
| Fan modes | Silent / Balanced / Turbo | hidden, with a line saying why |
| GPU | NVML (temp, load, power) | LibreHardwareMonitor for the temperature |
| Sound, Music, glass | full | full |

Running [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) in the
background is the one thing worth adding on an Intel or AMD machine: Blob picks its sensors up
automatically. The Settings page says what it found. The Sound page needs
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

## License

MIT — see [LICENSE](LICENSE).
