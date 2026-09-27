"""Blob's fully local handwriting setup and document tool."""

import ctypes
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image

import handwriting_generate
import handwriting_profile


HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))
APPDATA = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
DOWNLOADS = HOME / "Downloads"
PROFILE_HOME = handwriting_profile.PROFILE_HOME
WORK = APPDATA / "Blob-v5" / "handwriting" / "work"
OUT_ROOT = HOME / "Documents" / "Tareas a mano"
TASK_EXTENSIONS = handwriting_generate.SUPPORTED_EXTS
PHOTO_EXTENSIONS = handwriting_profile.IMAGE_EXTENSIONS
PROFILE_PAGES = tuple(handwriting_profile.PAGE_SPECS)
BROWSE_PAGE_SIZE = 8


def read_clipboard_text():
    """Read text locally from the Windows clipboard; never transmit it."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    for _ in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(.03)
    else:
        return None
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def dropped_files(drop_handle):
    """Return paths from WM_DROPFILES and always release the shell drop handle."""
    shell32 = ctypes.windll.shell32
    query = shell32.DragQueryFileW
    query.restype = wintypes.UINT
    query.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    finish = shell32.DragFinish
    finish.argtypes = [wintypes.HANDLE]
    finish.restype = None
    try:
        count = query(drop_handle, 0xFFFFFFFF, None, 0)
        paths = []
        for index in range(count):
            size = query(drop_handle, index, None, 0)
            buffer = ctypes.create_unicode_buffer(size + 1)
            if query(drop_handle, index, buffer, len(buffer)):
                paths.append(buffer.value)
        return paths
    finally:
        finish(drop_handle)


class HandwritingTool:
    """Thread-safe state for profile capture and local handwriting generation."""

    def __init__(self, clipboard=read_clipboard_text, downloads=DOWNLOADS,
                 profile_home=PROFILE_HOME, work=WORK, out_root=OUT_ROOT):
        self.clipboard = clipboard
        self.downloads = Path(downloads)
        self.profile_home = Path(profile_home)
        self.work, self.out_root = Path(work), Path(out_root)
        self.profile_info = handwriting_profile.profile_summary(self.profile_home)
        self.section = "write" if self.profile_info else "profile"
        self.profile_photos = {}
        self.profile_picker_page = None
        self.profile_building = False
        self.worksheet_busy = False
        self.worksheet_path = None
        self.profile_status = ("Local profile ready" if self.profile_info else "No handwriting profile yet")
        self.source = None
        self.source_label = ""
        self.source_meta = ""
        self.mode = "texto"
        self.ink = "negra"
        self.title = ""
        self.name = ""
        self.missing = ""
        self.status = "Create a local profile to start"
        self.progress = None
        self.busy = False
        self.pdf = None
        self.previews = []
        self.error = None
        self.version = 0
        self._seen = -1
        self._lock = threading.RLock()
        self.picker_open = False
        self.browse_filter = "task"
        self.browse_dir = self.downloads
        self.browse_entries = []
        self.browse_page = 0
        self.browse_loading = False
        self.browse_error = None
        self._browse_generation = 0
        self._source_check_generation = 0

    def _set(self, **values):
        with self._lock:
            for key, value in values.items():
                setattr(self, key, value)
            self.version += 1

    def poll(self):
        with self._lock:
            changed = self.version != self._seen
            self._seen = self.version
        return changed

    @property
    def profile_ready(self):
        return bool(self.profile_info)

    @property
    def ready(self):
        return self.source is not None and self.profile_ready and not self.busy and not self.missing

    @property
    def folder(self):
        return self.out_root / self.name if self.name else None

    @property
    def browse_root(self):
        return self.downloads if self.downloads.is_dir() else HOME

    @property
    def browse_root_label(self):
        return "Downloads" if self.downloads.is_dir() else "Home · Downloads unavailable"

    @property
    def browse_page_count(self):
        return max(1, (len(self.browse_entries) + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)

    def set_section(self, section):
        if section not in ("write", "profile"):
            return False
        if section == "write" and not self.profile_ready:
            self.section = "profile"
            self._set(error="Create a local handwriting profile before writing a task")
            return False
        self._set(section=section, error=None)
        return True

    # -- training worksheets and the private profile
    def create_worksheets(self):
        if self.worksheet_busy or self.profile_building:
            return False
        self._set(worksheet_busy=True, error=None, profile_status="Preparing print sheets on this PC…")
        threading.Thread(target=self._make_worksheets, daemon=True).start()
        return True

    def _make_worksheets(self):
        try:
            path = handwriting_profile.create_training_pdf(self.profile_home)
            try:
                os.startfile(str(path))
                status = "Practice sheets opened locally · print at 100%"
            except OSError:
                status = "Practice sheets are ready to open"
            self._set(worksheet_path=path, worksheet_busy=False, profile_status=status, error=None)
        except Exception as exc:
            self._set(worksheet_busy=False, profile_status="Could not create the practice sheets",
                      error=str(exc))

    def open_worksheets(self):
        path = self.worksheet_path or self.profile_home / "training" / "blob-handwriting-sheets.pdf"
        if not Path(path).is_file():
            return self.create_worksheets()
        try:
            os.startfile(str(path))
        except OSError as exc:
            self._set(error=f"Could not open the practice sheets: {exc}")
            return False
        return True

    def pick_profile_photo(self, page_id):
        if page_id not in PROFILE_PAGES or self.busy or self.profile_building:
            return False
        self.profile_picker_page = page_id
        self.open_picker("photo")
        return True

    def attach_profile_photo(self, page_id, path):
        if page_id not in PROFILE_PAGES:
            return False
        path = Path(path)
        if path.suffix.lower() not in PHOTO_EXTENSIONS or not path.is_file():
            self._set(error="Choose a JPG, PNG, WEBP, BMP, or TIFF scan")
            return False
        try:
            if path.stat().st_size > handwriting_profile.MAX_PHOTO_BYTES:
                self._set(error="Each handwriting photo must be under 40 MB")
                return False
            with Image.open(path) as image:
                if image.width * image.height > handwriting_profile.MAX_PHOTO_PIXELS:
                    self._set(error="This photo is too large. Choose a standard phone scan.")
                    return False
                image.verify()
        except (OSError, ValueError) as exc:
            self._set(error="This photo could not be opened. Choose another scan.")
            return False
        photos = dict(self.profile_photos)
        photos[page_id] = path
        self._set(profile_photos=photos, profile_picker_page=None,
                  picker_open=False, error=None,
                  profile_status=f"Added {handwriting_profile.PAGE_SPECS[page_id][0].lower()} scan")
        return True

    def build_profile(self):
        if self.profile_building or self.worksheet_busy:
            return False
        missing = [page for page in PROFILE_PAGES if page not in self.profile_photos]
        if missing:
            self._set(error="Add one clear photo for each of the three practice sheets")
            return False
        self._set(profile_building=True, busy=True, error=None, progress=(0, len(PROFILE_PAGES)),
                  profile_status="Reading your samples locally…", status="Building your local profile…")
        threading.Thread(target=self._build_profile_worker, args=(dict(self.profile_photos),), daemon=True).start()
        return True

    def _build_profile_worker(self, photos):
        def progress(done, total, message):
            self._set(progress=(done, total), profile_status=message)
        try:
            result = handwriting_profile.build_profile(photos, self.profile_home, progress=progress)
            info = handwriting_profile.profile_summary(self.profile_home)
            self._set(profile_info=info, profile_photos={}, profile_building=False, busy=False, section="write",
                      progress=None, error=None,
                      profile_status=f"Profile ready · {result['characters']} characters · saved on this PC",
                      status="Your handwriting profile is ready")
        except Exception as exc:
            self._set(profile_building=False, busy=False, progress=None,
                      profile_status="Profile not created · correct the scans and try again",
                      error=str(exc), status="Could not create the handwriting profile")

    # -- task source browser, limited to Downloads and kept inside Blob
    def use_clipboard(self):
        if self.busy:
            return False
        text = self.clipboard()
        if not text or not text.strip():
            self._set(error="The clipboard is empty", status="Copy task text first")
            return False
        self.work.mkdir(parents=True, exist_ok=True)
        path = self.work / "clipboard-task.txt"
        path.write_text(text, encoding="utf-8")
        self._adopt(path, "Clipboard", f"Text · {len(text):,} characters", "texto",
                    text.splitlines()[0][:48] if text.splitlines() else "Task", "Clipboard task")
        return True

    def pick_file(self):
        if self.busy:
            return False
        self.open_picker("task")
        return True

    def use_file(self, path):
        if self.busy:
            return False
        path = Path(path)
        ext = path.suffix.lower()
        if ext not in TASK_EXTENSIONS:
            self._set(error=f"Unsupported task file type: {ext or 'no extension'}")
            return False
        if not path.is_file():
            self._set(error="File not found. Choose another file.")
            return False
        try:
            size = path.stat().st_size
            if size > 30 * 1024 * 1024:
                self._set(error="Task files must be smaller than 30 MB")
                return False
        except OSError:
            self._set(error="This file can't be read. Choose another one.")
            return False
        mode = "tabla" if ext in TABLE_EXTENSIONS else "texto"
        title = path.stem.upper()[:48]
        self._adopt(path, path.name, f"{'Table' if mode == 'tabla' else 'Text'} · {ext[1:].upper()}",
                    mode, title, path.stem + " handwritten")
        return True

    def _adopt(self, path, label, meta, mode, title, name):
        self._source_check_generation += 1
        generation = self._source_check_generation
        self._set(source=Path(path), source_label=label, source_meta=meta, mode=mode,
                  title=title, name=name, missing="", error=None, pdf=None, previews=[],
                  progress=None, section="write", status="Checking this document locally…",
                  picker_open=False, profile_picker_page=None)
        threading.Thread(target=self._check_source_worker, args=(Path(path), mode, title, generation),
                         daemon=True).start()

    def _check_source_worker(self, path, mode, title, generation):
        try:
            content, _ = handwriting_generate.read_document(path, mode)
            if not self.profile_ready:
                self._set_if_source_current(generation, missing="", error=None,
                                            status="Create your local handwriting profile to write this task")
                return
            profile = handwriting_profile.load_profile(self.profile_home)
            missing = handwriting_generate.missing_glyphs(content + title, profile["glyphs"] if profile else {})
            if missing:
                shown = " ".join(repr(char) for char in missing[:12])
                more = " …" if len(missing) > 12 else ""
                values = dict(missing="".join(missing), error=f"Your profile is missing: {shown}{more}",
                              status="Add the missing characters to your profile and retry")
            else:
                values = dict(missing="", error=None, status="Ready · preview or create your PDF")
            self._set_if_source_current(generation, **values)
        except Exception as exc:
            self._set_if_source_current(generation, error=str(exc), status="Could not read this document")

    def _set_if_source_current(self, generation, **values):
        with self._lock:
            if generation != self._source_check_generation:
                return False
            for key, value in values.items():
                setattr(self, key, value)
            self.version += 1
            return True

    def set_mode(self, mode):
        if mode in ("texto", "tabla") and not self.busy:
            self._set(mode=mode, pdf=None, previews=[])
            if self.source:
                self._source_check_generation += 1
                generation = self._source_check_generation
                threading.Thread(target=self._check_source_worker,
                                 args=(self.source, mode, self.title, generation), daemon=True).start()

    def set_ink(self, ink):
        if ink in ("negra", "azul") and not self.busy:
            self._set(ink=ink, pdf=None, previews=[])

    # -- in-Blob Downloads picker
    def open_picker(self, browse_filter="task"):
        if self.busy or browse_filter not in ("task", "photo"):
            return False
        with self._lock:
            self.picker_open = True
            self.browse_filter = browse_filter
        return self.browse_to(self.browse_root)

    def close_picker(self):
        with self._lock:
            self.picker_open = False
            self.browse_loading = False
            self.profile_picker_page = None
            self._browse_generation += 1
            self.version += 1

    def browse_downloads(self):
        return self.browse_to(self.browse_root)

    def browse_parent(self):
        root = self.browse_root.resolve()
        parent = self.browse_dir.resolve().parent
        try:
            parent.relative_to(root)
        except ValueError:
            parent = root
        return self.browse_to(parent)

    def browse_to(self, folder):
        root = self.browse_root.resolve()
        folder = Path(folder).resolve()
        try:
            folder.relative_to(root)
        except ValueError:
            self._set(browse_error="Browse only inside Downloads")
            return False
        if not folder.is_dir():
            self._set(browse_error="That folder is no longer available")
            return False
        with self._lock:
            self._browse_generation += 1
            generation = self._browse_generation
            browse_filter = self.browse_filter
            self.browse_dir = folder
            self.browse_entries = []
            self.browse_page = 0
            self.browse_loading = True
            self.browse_error = None
            self.version += 1
        threading.Thread(target=self._read_browse_folder, args=(folder, generation, browse_filter),
                         daemon=True).start()
        return True

    @staticmethod
    def scan_folder(folder, browse_filter="task"):
        allowed = PHOTO_EXTENSIONS if browse_filter == "photo" else TASK_EXTENSIONS
        rows = []
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=True)
                    if is_dir or (entry.is_file(follow_symlinks=True)
                                  and Path(entry.name).suffix.lower() in allowed):
                        rows.append((Path(entry.path), is_dir))
                except OSError:
                    continue
        rows.sort(key=lambda row: (not row[1], row[0].name.casefold()))
        return rows

    def _read_browse_folder(self, folder, generation, browse_filter):
        try:
            entries = self.scan_folder(folder, browse_filter)
            error = None
        except OSError as exc:
            entries = []
            error = f"Can't read this folder: {exc.strerror or 'access denied'}"
        with self._lock:
            if generation != self._browse_generation or not self.picker_open:
                return
            self.browse_entries = entries
            self.browse_loading = False
            self.browse_error = error
            self.version += 1

    def move_browse_page(self, delta):
        with self._lock:
            self.browse_page = max(0, min(self.browse_page_count - 1, self.browse_page + delta))
            self.version += 1

    def open_browse_entry(self, index):
        with self._lock:
            if self.busy or self.browse_loading or not 0 <= index < len(self.browse_entries):
                return False
            path, is_dir = self.browse_entries[index]
            page_id = self.profile_picker_page
            browse_filter = self.browse_filter
        if is_dir:
            return self.browse_to(path)
        if browse_filter == "photo" and page_id:
            return self.attach_profile_photo(page_id, path)
        return self.use_file(path)

    def accept_drop(self, paths):
        paths = [Path(path) for path in paths]
        if self.busy:
            return False
        if len(paths) != 1:
            self._set(error="Drop one supported file at a time")
            return False
        if self.section == "profile" and self.profile_picker_page:
            return self.attach_profile_photo(self.profile_picker_page, paths[0])
        return self.use_file(paths[0])

    # -- local document rendering
    def preview(self):
        return self.ready and self._start_render(preview=True)

    def generate(self):
        return self.ready and self._start_render(preview=False)

    def _start_render(self, preview):
        if self.busy or not self.ready:
            return False
        self._set(busy=True, error=None, status="Preparing a local preview…" if preview else "Writing locally…",
                  progress=None, **({} if preview else {"pdf": None, "previews": []}))
        args = (self.source, self.profile_home, self.mode, self.ink, self.title,
                self.out_root, preview)
        threading.Thread(target=self._render_worker, args=args, daemon=True).start()
        return True

    def _render_worker(self, source, profile_home, mode, ink, title, out_root, preview):
        def progress(done, total, message):
            self._set(progress=(done, total), status=message)
        try:
            result = handwriting_generate.render_document(source, profile_home, mode, ink,
                                                          title, out_root, preview, progress)
            if preview:
                self._set(previews=result["previews"], pdf=None, progress=None,
                          status=f"Local preview ready · {result['pages']} page"
                          f"{'s' if result['pages'] != 1 else ''}", busy=False, error=None)
            else:
                size_mb = Path(result["pdf"]).stat().st_size / (1024 * 1024)
                self._set(pdf=result["pdf"], progress=None,
                          status=f"PDF ready · {result['pages']} page"
                          f"{'s' if result['pages'] != 1 else ''} · {size_mb:.1f} MB",
                          busy=False, error=None)
        except Exception as exc:
            self._set(error=str(exc), status="Could not finish · fix the issue and retry",
                      progress=None, busy=False)

    def open_result(self):
        target = self.pdf or (self.previews[0] if self.previews else None)
        if target and Path(target).is_file():
            os.startfile(str(target))
            return True
        return False

    def open_folder(self):
        folder = self.folder
        if folder and folder.is_dir():
            os.startfile(str(folder))
            return True
        return False


def available():
    """The generic profile builder ships with Blob and needs no private user folder."""
    return Path(__file__).is_file()
