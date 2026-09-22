# Blob 5.0.0 — Liquid Glass tools and direct MiniBlob movement

Blob v5 keeps the paired SmallBlob and Dashboard host while updating the compact utility layer.
The release is tagged `v5.0.0`; the earlier `v4.0.1` and `v3.0.0` tags remain intact.

## What changed

- MiniBlob moves directly from either its primary body or inset restore satellite. A short release
  retains the body's normal action; dragging repositions it. The cursor remains a normal arrow.
- The radial Calculator, Clipboard, Magnifier and blank future-tool slot remain click-only, so the
  utility palette cannot accidentally drag the bubble.
- Palette lobes use continuous smooth unions and constant-radius round caps, eliminating pointed
  extrusion spikes while keeping the glass refraction continuous.
- Calculator, Clipboard and Magnifier cards keep their saved session data and monitor-safe layout.

## Migration and installation

- The v5 profile is `%LOCALAPPDATA%\Blob-v5`; first run copies the newest valid preferences from
  `%LOCALAPPDATA%\Blob-v4`, then falls back to `%LOCALAPPDATA%\Blob-v3`.
- Legacy profile files are read-only migration inputs. System-wide audio processing stays off on a
  fresh v5 launch until you explicitly enable it.
- The installer uses `%LOCALAPPDATA%\Programs\Blob-v5` and creates a **Blob v5** Desktop shortcut.

## Verification boundary

`python -m unittest discover -s tests -p "test_*.py" -q` covers the paired host, direct body and
satellite dragging, tool-palette click isolation, playback gestures, palette geometry and GPU
shader compilation against synthetic fixtures. It does not claim live game, audio-routing or
sensor-provider certification.
