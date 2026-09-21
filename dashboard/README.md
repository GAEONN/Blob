# Blob Dashboard

Open **Launch Blob Dashboard.cmd** in the parent folder, or `pythonw dashboard/app.pyw`.
Keep this folder beside the existing Blob modules. In v4 this is a presentation of the
unified app, not a second process. F10 or the tray menu switches to SmallBlob.

The overview shows Music, System, Sound and Gaming at once. Click **Game dock** to detach
the narrow overlay to the left. It starts click-through: **Ctrl+Alt+V** (or your migrated shortcut) unlocks it. Click
its satellite to expand downward, the top chevron to collapse, and **Blob** to return.
Lock it again before playing. F11 switches the dashboard's large/windowed size.

Settings and controllers are shared with SmallBlob in v4. Audio playback/volume/boost
control Windows' shared media and audio services. Avoid multiple enhancers at once.

## Checks

From the parent folder:

```
python -m unittest discover -s dashboard -p test_dashboard.py -v
python dashboard/render_preview.py
python -m unittest discover -s tests -q
```

The review images use synthetic artwork and telemetry, not the user's real media.
The GPU renderer is the production renderer with a process-local top-left anchor; the
SmallBlob shader file is not edited. The dock uses a small render buffer so it does not
keep reading back a full-screen image during gaming.
