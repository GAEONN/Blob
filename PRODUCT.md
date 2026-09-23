# Blob

## v5 application host

`app.pyw` / `unified.py` pair the SmallBlob layouts below with the full Dashboard in one
process and one native window. Tray mode commands or local F10 switch presentations without
recreating audio/media/telemetry controllers. The app uses a single `%LOCALAPPDATA%\Blob-v5`
profile, isolated from legacy versions. Shared options carry across views; layout state stays
per-view. Dashboard's game dock starts locked. See [V5-NOTES.md](docs/V5-NOTES.md) for release,
installation and verification boundaries. The SmallBlob behavior descriptions below apply
within the compact presentation unless explicitly overridden by this host contract.

Blob is a native Windows liquid-glass tray control panel built with Python, OpenGL
and a layered Win32 window. It combines vendor-neutral read-only hardware monitoring, sound
enhancement and music controls. Hardware support varies by sensor/provider. Music controls work
with any Windows media session; Apple Music queue operations are an optional extension when its
Windows app is already installed.

## System card and metrics bubble

- The first view is labeled System. Its card prioritizes CPU/GPU temperature and fan RPM, offers
  Auto, Quiet, Balanced, Turbo and Custom preferences, and expands in place for the full
  bounded sensor inventory.
- The contextual corner control springs the card into a fused two-circle glass bubble. The bubble
  shows CPU and GPU temperatures—the useful glance view—while the full card retains the five
  monitoring preferences and bounded sensor inventory. The main body keeps the legacy quick-mode
  action; the visible inset satellite restores the System card. Both are direct bubble drag handles:
  a short release preserves their normal action, while a real move repositions MiniBlob.
  Mode choices persist, but are explicitly described as monitoring-only until a real fan-profile
  backend is connected; Blob never pretends that a firmware or pump setting changed.
- The top-right anchor stays fixed during the morph. Bubble placement follows Blob's shared global
  lock state. Every MiniBlob uses its primary body and inset satellite for direct dragging without
  switching to a four-way move cursor; tool-palette lobes stay click-only.

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

## Sound status bubble

- Sound exposes the same compact bubble affordance in Regular and Compact cards. It shows the
  current boost level and on/off state, with a visible inset satellite that restores the full Sound
  card. Its body toggles enhancement; the full card remains the place for output, presets and EQ.

## Shared overlay lock

- Blob starts unlocked. Tab changes, entering Gaming, hiding and reopening do not mutate that state.
- The configured bind and tray command are the authoritative lock toggle. Locking makes every view
  click-through; unlocking restores normal controls and bubble dragging.

## Gaming and queue extension

- Gaming adds the approved slim, draggable strip: FPS and frame time, CPU/GPU
  temperatures and utilization, RAM, fan RPM and GPU power. It keeps Blob's existing
  glass surface and provides navigation through its edge menu without activating
  the strip over the foreground application. Windowed/borderless use is intended;
  visibility over exclusive fullscreen is not guaranteed.
- SmallBlob's Gaming view offers the same six FPS, frame-time and system metrics in a
  horizontal strip, a vertically stacked strip, or a compact FPS/frame-time Bubble. The
  strip menu reuses the main app's morphing tab dropdown and follows the side of the monitor
  where the strip is parked. Dropping a free Gaming strip at the top work-area edge snaps it
  horizontal; dropping it at either side edge snaps it vertical. Every SmallBlob size spring,
  tool expansion and monitor transfer clamps the complete glass surface and its action rail
  inside the current monitor work area.
  Settings → Overlay → Gaming view selects Horizontal, Vertical, or Bubble. Bubble remembers
  the last strip orientation, and its inset satellite restores that strip with the same spring
  morph instead of adding another minimizer button to the strip.
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

Current v5 evidence: `blob.pyw`, `unified.py`, `glass.py`, `reactive.py`, `engine.py`,
`docs/V4-NOTES.md` and `.impeccable/surfaces/blob-pyw.md`. Preview art and telemetry are synthetic test fixtures, not new
shipping assets. Provider fixtures and the reported local ASUS check are not multi-PC testing
or certification. This documentation reconciliation adds no runtime validation claim.
