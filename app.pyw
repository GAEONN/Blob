import os
import sys

# Fast path: Blob is already running in the tray. Bring it up with a window message before
# importing anything heavy (numpy, PIL, the renderer), so the shortcut feels instant.
if "--startup" not in sys.argv:
    import ctypes
    from ctypes import wintypes
    _u = ctypes.windll.user32
    _u.FindWindowW.restype = wintypes.HWND
    _u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _hwnd = _u.FindWindowW("BlobGlass", "Blob v6")
    if _hwnd:
        _view = sys.argv[sys.argv.index("--view") + 1] if "--view" in sys.argv[:-1] else None
        _msg = {"small": 0x8010, "dashboard": 0x8011}.get(_view, 0x8013)  # SMALL / FULL / OPEN
        _u.AllowSetForegroundWindow(-1)  # ASFW_ANY: let Blob take focus from this launcher
        if _u.PostMessageW(_hwnd, _msg, 0, 0):
            sys.exit(0)

# numpy/scipy's OpenBLAS otherwise spawns a thread per CPU and reserves ~1.5 GB; inherited by the audio engine
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

from unified import main

if __name__ == '__main__':
    main()
