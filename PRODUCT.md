# Blob

## v4 application host

`app.pyw` / `unified.py` pair the SmallBlob layouts below with the full Dashboard in one
process and one native window. Tray mode commands or local F10 switch presentations without
recreating audio/media/telemetry controllers. The app uses a single `%LOCALAPPDATA%\Blob-v4`
profile, isolated from legacy versions. Shared options carry across views; layout state stays
per-view. Dashboard's game dock starts locked. See [V4-NOTES.md](V4-NOTES.md) for release,
installation and verification boundaries. The SmallBlob behavior descriptions below apply
within the compact presentation unless explicitly overridden by this host contract.

Blob is a native Windows liquid-glass tray control panel built with Python, OpenGL
and a layered Win32 window. It combines vendor-neutral read-only hardware monitoring, sound
enhancement and music controls. Hardware support varies by sensor/provider. Music controls work
with any Windows media session; Apple Music queue operations are an optional extension when its
Windows app is already installed.

## System card and mode bubble

- The first view is labeled System. Its card prioritizes CPU/GPU temperature and fan RPM, offers
  Auto, Quiet, Balanced, Turbo and Custom preferences, and expands in place for the full
  bounded sensor inventory.
- A single-circle contextual corner control springs the card into a fused two-circle glass bubble.
  Width, height, silhouette and content animate as one continuous morph. The main body
  cycles the four quick modes and the attached satellite restores the full interface. Mode choices
  persist, but are explicitly described as monitoring-only until a real fan-profile backend is
  connected; Blob never pretends that a firmware or pump setting changed.
- The top-right anchor stays fixed during the morph. Bubble placement follows Blob's shared global
  lock state. Only the satellite restores or drags the bubble and shows the move cursor when
  unlocked; the main body remains a mode-cycle target.

## Music card and player bubble

- Regular Music restores the cover-first layout at 340 × 582 DIP with 296-DIP square art.
  Compact is 248 × 216 DIP with a 54-DIP square thumbnail and a 6-DIP continuous corner.
  Both expose Search, Playing Next and filled transport; Regular has a volume slider below
  transport. The optional bubble retains the existing continuous spring morph.
- Full-cover mode is 340 DIP square in either size, with a uniform 4-DIP rim and a persistent
  contrast-backed return control. Its hover overlay retains Volume and Options utility anchors;
  these inline utilities are not the Regular/Compact card layout.
- The player bubble responds to relative audio transients; a steady loud signal
  settles rather than holding the surface inflated. The bubble's main lobe supports tap to
  play or pause, double-tap for next, and hold for previous; its attached satellite restores the
  Music card on click and repositions the bubble when dragged. Playback comes from the current
  Windows media session and remains service-neutral.
  The shared overlay shortcut controls the same lock state on every tab.
- Reactiveness is enabled by default and persisted through the equalizer toggle in Music Options or
  the matching Settings switch. Bass deforms the main lobe, mids flex the fused neck and treble
  moves the satellite. Springs and hard shader bounds preserve click geometry. When DSP spectrum is
  unavailable, read-only peak meters across all active Windows playback endpoints supply an
  amplitude-reactive fallback without trusting a potentially stale player transport status.
  This fallback uses the same amplitude input for all three envelopes, not measured frequency
  bands or fabricated beats. Full-cover mode stays stationary. Its visualizer button toggles
  in-cover bars, using DSP bins when available and a uniform amplitude meter otherwise.

## Shared overlay lock

- Blob starts unlocked. Tab changes, entering Gaming, hiding and reopening do not mutate that state.
- The configured bind and tray command are the authoritative lock toggle. Locking makes every view
  click-through; unlocking restores normal controls and bubble dragging.

## Gaming and queue extension

- Gaming adds the approved slim, draggable strip: FPS and frame time, CPU/GPU
  temperatures and utilization, RAM, fan RPM and GPU power. It keeps Blob's existing
  glass surface and provides navigation through its left menu without activating
  the strip over the foreground application. Windowed/borderless use is intended;
  visibility over exclusive fullscreen is not guaranteed.
- The optional 104 × 98 DIP FPS bubble shows only FPS and frame time in ms. Its satellite
  restores the strip or drags while unlocked, and is its only move-cursor target.
  Settings → Overlay → Gaming view also selects Strip / FPS bubble.
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

## Settings, sensor scope and verification

Settings → Appearance → Size selects Regular / Compact. Settings wraps labels and hints into
variable-height rows, scrolls by complete rows, and gives the shortcut input its own row.

Sensor feeds merge per field: LibreHardwareMonitor/OpenHardwareMonitor and HWiNFO can supplement
one another; ASUS supplies an optional read-only fallback. Availability depends on hardware,
drivers and provider support, not a universal fan API. Fan names, provider and sensor IDs are
retained; deduplication uses provider plus sensor ID, so equal labels or RPM do not collapse
distinct sensors. A stopped fan remains 0 RPM; missing and invalid readings remain unavailable.
This source contains no fan-control writes; mode choices are monitoring preferences.

Current v4 evidence: `blob.pyw`, `unified.py`, `glass.py`, `reactive.py`, `engine.py`,
`V4-NOTES.md` and `.impeccable/surfaces/blob-pyw.md`. Preview art and telemetry are synthetic test fixtures, not new
shipping assets. Provider fixtures and the reported local ASUS check are not multi-PC testing
or certification. This documentation reconciliation adds no runtime validation claim.
