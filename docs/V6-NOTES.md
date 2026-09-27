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
