# Blob

Blob is a native Windows liquid-glass tray control panel built with Python, OpenGL
and a layered Win32 window. It combines hardware monitoring/fan controls, sound
enhancement and music controls. Hardware support varies by sensor/provider; Apple
Music queue operations use the installed Windows app through UI Automation.

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
