# Blob Dashboard

**v4 host override:** This presentation now runs inside `../unified.py` alongside SmallBlob,
with one set of controllers and `%LOCALAPPDATA%\Blob-v4` settings. F10/tray commands switch
views; the shared default lock shortcut is Ctrl+Alt+V. The isolation description below is
the preserved v3 standalone architecture, not how the v4 entry points launch. See V4-NOTES.md.

A separate Windows companion for SmallBlob. The user's pinned model is a car-display /
CarPlay-like overview: Music, System temperatures/fans, Sound controls and Gaming data
are visible simultaneously. It inherits Blob's live refractive glass, normal readable
type, continuous corners and restrained motion, not a new brand identity.

## Isolation

The existing parent files are imported read-only. The new entry point is `app.pyw`.
Dashboard uses its own process, `Local\BlobDashboard` mutex, tray title and
`%LOCALAPPDATA%\Blob-Dashboard` settings. It does not change SmallBlob code, preferences
or running instance. The services still read the same Windows media session and hardware;
audio routing is system-wide, so enabling boost is a system action, not a private mixer.
Boost is off by default. Do not run multiple audio enhancers simultaneously.

## Behaviors

- Opens across the current monitor's work area. F11 or the header window button switches
  to a smaller landscape window. Settings stays beside Music, replacing the right column.
- Real Windows media playback, seeking, shuffle/repeat, Apple Music Search / Playing Next
  reuse the existing providers. Apple Music integration still requires its installed app.
- Live read-only hardware telemetry is provider-dependent; missing sensors remain dashes.
  No universal fan-control promise or firmware writes.
- Game dock starts click-through. Ctrl+Alt+D toggles its input lock. Its 92-DIP fused
  bubble expands downward into a 92×480-DIP dock, retaining its top-left anchor.
  The satellite expands it; the top chevron collapses it; Blob returns to the overview.
  Expanded dock shows FPS, ms, CPU/GPU temperature and GPU power.
- FPS comes from PresentMon, not display refresh rate; permission and exclusive-fullscreen
  limitations from SmallBlob still apply.
- No autonomous whole-cover motion. Optional local audio bars use actual DSP bins or a
  uniform endpoint-amplitude fallback. There are no fabricated bands or telemetry.

This is native Windows/Python/OpenGL, not a browser site. Validation uses synthetic
fixture captures through the real shader plus a real process/window startup check.
