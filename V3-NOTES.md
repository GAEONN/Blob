# Blob v3

V3 is an isolated local checkout built from `4f78076`. V1 and Yao's V2 stay intact.
It has its own tray name, configuration folder (`%LOCALAPPDATA%\Blob-v3`), single-instance
mutex and default lock shortcut: **Ctrl+Alt+V**. It is not pushed to GitHub yet.

## Changes

- Restored V1's cover-first regular Music player and 248-DIP compact player.
  Settings → Appearance → Size selects Regular / Compact. Album thumbnails stay square
  with a 6-DIP continuous corner, rather than turning into rounded tiles.
- Optional music bubble responds to relative audio accents. A steady loud
  signal settles rather than staying inflated. The endpoint-meter fallback is amplitude-only;
  frequency-specific motion uses the DSP spectrum when available. No fabricated beats.
- Full cover is square with a uniform 4-DIP glass rim. A persistent, contrast-backed return
  control works over light covers. The cover stays stationary; its visualizer button toggles
  in-cover bars, with real DSP bins or a uniform amplitude meter when boost is off.
- Hardware is labeled System; only bubble satellites show the move cursor.
- Gaming supports Strip / FPS bubble. The bubble shows FPS and frame time only. Use its
  satellite to return, or Settings → Overlay → Gaming view. Lock the overlay before playing.
- Compact/regular Settings rows wrap with explicit spacing; shortcut input has its own row.
- Sensor feeds are combined per field. One provider's temperatures no longer mask another's
  fans. Stopped fans remain 0 RPM, labels are preserved, invalid/stale values are rejected.
  HWiNFO's packed header and reading offsets are corrected. ASUS has an optional read-only
  fallback; other brands continue to use LibreHardwareMonitor/OpenHardwareMonitor or HWiNFO.

## Compatibility limits

Windows does not provide every computer's fan RPM or fan controls through one universal API.
Readings depend on hardware, drivers and supported sensor providers; unavailable data remains
unavailable. No firmware controls, unsafe probing, or anti-cheat injection were added.
Profile selections remain monitoring preferences, not real fan-speed controls.

Sources: [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor),
[HWiNFO SDK structure definitions](https://github.com/MintyMods/MintySensorMonitor/blob/master/hwinfo/hwisenssm2.h_v6.11.3917),
[Windows peak meter](https://learn.microsoft.com/en-us/windows/win32/api/endpointvolume/nn-endpointvolume-iaudiometerinformation).

## Verification

`python -m unittest discover -s tests -q`

`python tests/render_preview.py --output v3-review.tmp.png`

`python tests/render_preview.py --gaming --output v3-gaming.tmp.png`

`python tests/render_v3_motion.py`

Preview artwork and telemetry are synthetic fixtures, rendered through the real GPU shader.
No new shipping image assets. The local ASUS read-only fallback was tested on the host;
Intel/AMD/NVIDIA provider combinations are covered by fixtures, not a multi-PC certification.
