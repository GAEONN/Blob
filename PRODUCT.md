# Blob

Blob is a native Windows liquid-glass tray control panel built with Python, OpenGL
and a layered Win32 window. It combines vendor-neutral read-only hardware monitoring, sound
enhancement and music controls. Hardware support varies by sensor/provider. Music controls work
with any Windows media session; Apple Music queue operations are an optional extension when its
Windows app is already installed.

## Hardware card and mode bubble

- The first view is labeled Hardware. Its card prioritizes CPU/GPU temperature and fan RPM, offers
  Auto, Quiet, Balanced, Turbo and Custom preferences, and expands in place for the full
  bounded sensor inventory.
- A single-circle contextual corner control springs the card into a fused two-circle glass bubble.
  Width, height, silhouette and content animate as one continuous morph. The main body
  cycles the four quick modes and the attached satellite restores the full interface. Mode choices
  persist, but are explicitly described as monitoring-only until a real fan-profile backend is
  connected; Blob never pretends that a firmware or pump setting changed.
- The top-right anchor stays fixed during the morph. Bubble placement is locked by default and
  becomes draggable through the same user-editable overlay shortcut used by Gaming.

## Music card and player bubble

- The Music card keeps artwork, metadata, timeline and transport clear of its unmarked corner
  bubble control. The same continuous width, height, content and silhouette springs contract it
  toward that top-right control instead of replacing the card with a flick.
- Mini-card and cover modes share two stable utility anchors: Volume on the left and Options on the
  right. Each replaces only the middle control row—Volume with a system slider, Options with Search,
  Playing Next, Shuffle and Repeat—so the surface gains capability without permanent clutter.
- The player bubble breathes subtly with the live audio spectrum. Its main lobe supports tap to
  play or pause, double-tap for next, and hold for previous; its attached satellite restores the
  Music card. Playback comes from the current Windows media session and remains service-neutral.
  The shared overlay shortcut unlocks the bubble for dragging and relocks it for transport gestures.

## Gaming and queue extension

- Gaming adds the approved slim, draggable strip: FPS and frame time, CPU/GPU
  temperatures and utilization, RAM, fan RPM and GPU power. It keeps Blob's existing
  glass surface and provides navigation through its left menu without activating
  the strip over the foreground application. Windowed/borderless use is intended;
  visibility over exclusive fullscreen is not guaranteed.
- FPS/frame time require the optional PresentMon console helper. `gaming.py`
  derives them from application presentation intervals for the external foreground
  process, selecting one swapchain rather than combining them. Missing/stale frame
  data stays unavailable; monitor refresh rate is never substituted. Capture runs
  only while Gaming is visible. Hardware readings retain existing sensor limits.
- Music's Playing Next reads Apple Music's queue, keeps cached rows during refresh,
  coalesces requests and fetches covers independently. Refreshes follow entry,
  periodic queue viewing and detected track changes while Music is open. Queue
  availability depends on Apple Music; artwork depends on catalog/network access.

Evidence: `blob.pyw`, `gaming.py`, `applemusic.py`, `README.md` and
`.impeccable/surfaces/blob-pyw.md`. The handoff reports real desktop application
PresentMon frames observed, not an end-to-end gameplay FPS test. This documentation
pass does not assert additional runtime or test results.
