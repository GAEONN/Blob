# Blob 4.0.1 — Archived paired-view release

This is the preserved v4 release note. Blob v5 is the current release; see [V5-NOTES.md](V5-NOTES.md).

SmallBlob v3 and the large Dashboard are now two views of **one running application**.
One tray icon, native window, monitor, audio controller, music session and FPS monitor are
kept alive when switching. The GPU context is reused and its buffers resize for the active
view; there is no duplicate full-screen renderer hidden behind SmallBlob.

## Open and switch

- Launch `app.pyw`, `Launch Blob.cmd`, or the installed **Blob v4** Desktop shortcut.
- Right-click the tray → SmallBlob, Dashboard or Game dock.
- F10 switches SmallBlob / Dashboard only while Blob has keyboard focus.
- F11 changes Dashboard's full/windowed size. The game dock still expands downward.
- Ctrl+Alt+V is the default shared lock shortcut; migrated custom shortcuts are retained.
- `Launch SmallBlob.cmd` and `Launch Blob Dashboard.cmd` request that view in the same
  running instance. They do not launch competing audio engines.

## Preserved versions and settings

- At v4 publication, `main` pointed at this release; `v4.0.1` remains preserved as its tag.
- Tag `v3.0.0` preserves the exact pre-pairing v3 plus standalone Dashboard source.
- The paired application began at `v4.0.0`; this patch is tagged `v4.0.1` and is promoted to `main`.
- `v4.0.1` adds the glass identity mark to the tray, app icon and Desktop shortcut, and refreshes
  repository-facing product language to describe the vendor-neutral unified app.
- Remote installation uses `%LOCALAPPDATA%\Programs\Blob-v4`, not the old installation.
- The v4 profile is `%LOCALAPPDATA%\Blob-v4`. First run copies v3 preferences read-only;
  legacy profiles are read-only migration inputs. Audio boost is off on first v4 launch.
- SmallBlob's visual layout and interactions are unchanged. Only its direct script entry
  now routes to the unified host. The old v3 checkout remains untouched.
- Glass, screenshot preference, visualizer, sound settings and overlay shortcut are shared;
  each view retains its own layout/navigation in the current session. Last app view persists.

## Verification and limits

Offline tests cover repeated view switching, service identity, one-time controller
construction, the shared tray, local-only F10, profile migration, frozen capture, dock
geometry, and compilation/rendering/resizing of the real GPU shader with native window
output and desktop capture mocked. No user windows, games, audio routing, mouse or keyboard
were operated during this integration. Live end-to-end switching and gameplay still need
user verification; automated coverage is not a hardware certification.

Hardware readings depend on supported providers. There is no universal fan-control API.
Exclusive-fullscreen overlay visibility is not guaranteed. Apple Music queue/search still
requires its Windows app. Capture inclusion freezes the backdrop to avoid recursive glass.
Audio and optional sensor/FPS integrations are shared Windows resources: do not run v3 or the
standalone Dashboard alongside v4. FxSound and v4 cannot own the Windows default audio route
simultaneously; the Sound handoff closes FxSound before enabling Blob and restores the prior
device when Blob is disabled.

This release contains Windows Python source plus its installer, not a signed standalone EXE.
