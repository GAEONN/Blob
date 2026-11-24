# Blob v6.1.0 — Faster, smoother, louder

## Launch
- Opens in ~1.2–1.7 s instead of 3.6–4.5 s: removed a fixed 1.2 s wait and moved scipy /
  PortAudio imports out of the UI process (only the audio engine loads them).
- The **Blob** shortcut (Desktop and Start menu) brings a Blob already in the tray to the front
  in ~0.1 s, before any heavy import.

## Motion
- A high-resolution frame pacer replaces the 16 ms `SetTimer`, which really ran at ~40 FPS with
  15/16/31 ms gaps. Frames are now a steady 60 FPS.
- Desktop capture for the glass runs on a background thread while the panel stands still
  (frame cost 10 ms → 3 ms); moving/resizing still captures synchronously so refraction stays aligned.
- The once-a-second data refresh and tray-icon update no longer hitch animations.
- The page switcher no longer stutters: cached album art, memoised text wrapping, switcher-only
  redraws and skipped unchanged texture uploads (late frames on Music 28 % → ~3 %).
- One motion vocabulary: panel width, height and morphs share one spring, so the glass no
  longer skews while resizing; presses, hovers, pills and toggles each share one feel.
- Text ink is one colour for the whole panel, chosen with hysteresis and eased, instead of
  flipping per line over two-tone backgrounds.
- MiniBlob can be dragged right up to the screen edge; tool-palette lobes are slimmer.

## Sound
- New engine: sub-sonic cut, psychoacoustic bass (audible on laptop speakers), stronger
  clarity, bass-safe stereo widening, a leveler, up to +18 dB drive into a 1.3 ms look-ahead
  limiter and a soft clipper. The old boost was undone by its own limiter.
- Presets are full recipes (EQ + Bass/Clarity/Surround) and a **Custom** preset keeps your edits.
- New **Equalizer** tab: 10 bands, ±12 dB.

## Fixes
- Switching to Dashboard (F10) failed silently after a fractional window position.
- `VERSION` records `6.1.0`; release tag: `v6.1.0`.

# Blob v6.0.0 — Private handwriting and local writing tools

Blob v6 adds a fully local, personal handwriting workflow to the existing SmallBlob and Dashboard
application. The release keeps the v5 install/data directories and single-instance mutex so an
upgrade replaces the existing app without losing settings or starting a parallel Blob process.

## Handwriting tool

- Create printable lowercase, capitals/digits, and punctuation practice sheets in Blob.
- Write every prompted character six times. Blob locally rectifies the three photos and extracts
  six distinct glyph variants per character for the personal profile.
- Select photos and supported writing tasks in Blob's built-in browser; dropping supported files on
  the task area does not open File Explorer.
- The original photos are read in place and are not copied into the app or profile folder. Only
  extracted glyph images and their manifest are saved in the user's Local AppData profile.
- Generate handwriting-style documents locally. Personal photos, glyphs and resulting documents
  are excluded from this repository and are not sent to a handwriting service.

## Upgrade and compatibility

- The desktop shortcut is **Blob v6**. Updating replaces the existing application in place.
- Existing preferences and local tool data remain under `%LOCALAPPDATA%\Blob-v5`; the v5 mutex
  name is intentionally retained to prevent a v5/v6 double launch during upgrade.
- `VERSION` records `6.0.0`; release tag: `v6.0.0`.

## Verification boundary

Automated regression tests cover the local profile workflow, six-sample validation and storage,
document generation, built-in file browsing, monitor-safe layout, and the existing app/dashboard
suite. This does not claim hardware-wide handwriting accuracy; scans must contain all samples and
the worksheet registration marks.
