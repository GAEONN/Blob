# Blob Dashboard

**v5 host:** This presentation runs inside `../unified.py` alongside SmallBlob, with one set of
controllers and `%LOCALAPPDATA%\Blob-v5` settings. F10/tray commands switch views; the shared
default lock shortcut is Ctrl+Alt+V. `dashboard/app.pyw` requests this view from the unified
entry point. See ../docs/V5-NOTES.md.

The full-screen Windows presentation for SmallBlob. The user's pinned model is a car-display /
CarPlay-like overview: Music, System temperatures/fans, Sound controls and Gaming data
are visible simultaneously. It inherits Blob's live refractive glass, normal readable
type, continuous corners and restrained motion, not a new brand identity.

## Shared host

Dashboard shares the unified v5 process, `Local\BlobUnified-v5` mutex, tray title,
`%LOCALAPPDATA%\Blob-v5` settings, media session, hardware monitor and audio controller with
SmallBlob. The compatibility entry point is `app.pyw`; it requests the Dashboard view and does
not create a second engine. Audio routing is system-wide, so enabling boost is a system action,
not a private mixer.
Boost is off by default. Do not run multiple audio enhancers simultaneously.

## Behaviors

- Opens across the current monitor's work area. F11 or the header window button switches
  to a smaller landscape window. Settings stays beside Music, replacing the right column.
- Real Windows media playback, seeking, shuffle/repeat, Apple Music Search / Playing Next
  reuse the existing providers. Apple Music integration still requires its installed app.
- Live read-only hardware telemetry is provider-dependent; missing sensors remain dashes.
  No universal fan-control promise or firmware writes.
- Game dock starts click-through. Ctrl+Alt+V toggles its input lock. Its 92-DIP fused
  bubble expands downward into a 92×480-DIP dock, retaining its top-left anchor.
  The satellite expands it; the top chevron collapses it; Blob returns to the overview.
  Expanded dock shows FPS, ms, CPU/GPU temperature and GPU power.
- FPS comes from PresentMon, not display refresh rate; permission and exclusive-fullscreen
  limitations from SmallBlob still apply.
- No autonomous whole-cover motion. Optional local audio bars use actual DSP bins or a
  uniform endpoint-amplitude fallback. There are no fabricated bands or telemetry.

This is native Windows/Python/OpenGL, not a browser site. Validation uses synthetic
fixture captures through the real shader plus a real process/window startup check.
