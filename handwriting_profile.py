"""Create and use private, on-device handwriting glyph profiles for Blob.

This module contains the generic workflow only. User photographs and extracted
glyphs live below Local AppData, never beside the application source.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import uuid

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from scipy import ndimage


APPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
PROFILE_HOME = APPDATA / "Blob-v5" / "handwriting"
SHEET_SIZE = (2550, 3300)  # US Letter at 300 dpi
TARGET_X_HEIGHT = 32
SAMPLES_PER_CHARACTER = 6
PROFILE_SCHEMA = 2
MAX_PHOTO_BYTES = 40 * 1024 * 1024
MAX_PHOTO_PIXELS = 50_000_000
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

PAGE_SPECS = {
    "lower": ("Lowercase", tuple("abcdefghijklmnopqrstuvwxyzñáéíóúü")),
    "upper": ("Capitals + digits", tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZÑÁÉÍÓÚÜ0123456789")),
    "marks": ("Punctuation", tuple(".,;:!?¿¡'\"-–—()[]{}_/\\@#%&+=€$")),
}
REQUIRED_CHARACTERS = frozenset(char for _, chars in PAGE_SPECS.values() for char in chars)
LOWER_X_HEIGHT_CHARS = set("acemnorsuvwxz")


class ProfileBuildError(ValueError):
    """A clear, recoverable problem with a handwriting worksheet or photo."""


def _font(size, bold=False):
    names = ("seguisb.ttf", "arialbd.ttf", "segoeui.ttf", "arial.ttf") if bold else (
        "segoeui.ttf", "arial.ttf", "seguisb.ttf")
    for name in names:
        for folder in (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts",
                       Path("/usr/share/fonts/truetype/dejavu")):
            try:
                return ImageFont.truetype(str(folder / name), size)
            except OSError:
                continue
    return ImageFont.load_default()


def sheet_layout(page_id):
    """Return deterministic sample rectangles and baselines for a worksheet."""
    if page_id not in PAGE_SPECS:
        raise ProfileBuildError("Unknown handwriting worksheet")
    chars = PAGE_SPECS[page_id][1]
    width, height = SHEET_SIZE
    cols, rows = 7, math.ceil(len(chars) / 7)
    margin, top, bottom = 145, 465, 3175
    cell_w = (width - 2 * margin) / cols
    cell_h = (bottom - top) / rows
    cells = []
    for index, char in enumerate(chars):
        row, col = divmod(index, cols)
        x0 = round(margin + col * cell_w)
        y0 = round(top + row * cell_h)
        x1 = round(margin + (col + 1) * cell_w)
        y1 = round(top + (row + 1) * cell_h)
        h = y1 - y0
        # The printed prompt sits outside the masked area; six separated
        # writing lanes provide independent variants for every character.
        sample_w = (x0 + 66, x1 - 16)
        sample_top = y0 + round(h * .20)
        sample_bottom = y0 + round(h * .94)
        slot_h = (sample_bottom - sample_top) / SAMPLES_PER_CHARACTER
        samples = []
        for sample_index in range(SAMPLES_PER_CHARACTER):
            slot_top = round(sample_top + sample_index * slot_h)
            slot_bottom = round(sample_top + (sample_index + 1) * slot_h)
            baseline = round(slot_top + (slot_bottom - slot_top) * .78)
            rect = (sample_w[0], slot_top + 1, sample_w[1], slot_bottom - 1)
            samples.append((rect, baseline))
        cells.append({"char": char, "cell": (x0, y0, x1, y1),
                      "samples": tuple(samples)})
    return cells


def _registration_marks(draw):
    size, inset = 82, 82
    w, h = SHEET_SIZE
    points = ((inset + size / 2, inset + size / 2),
              (w - inset - size / 2, inset + size / 2),
              (w - inset - size / 2, h - inset - size / 2),
              (inset + size / 2, h - inset - size / 2))
    for x, y in points:
        draw.rectangle((round(x - size / 2), round(y - size / 2),
                        round(x + size / 2), round(y + size / 2)), fill=(15, 18, 24))
    return points


def render_training_sheets():
    """Build three generic, printable sheets; no personal data is read or used."""
    pages = []
    for page_id, (title, _) in PAGE_SPECS.items():
        image = Image.new("RGB", SHEET_SIZE, (255, 255, 255))
        draw = ImageDraw.Draw(image)
        marks = _registration_marks(draw)
        draw.text((190, 155), "Blob · My handwriting", fill=(22, 28, 36), font=_font(64, True))
        draw.text((190, 245), title, fill=(35, 42, 52), font=_font(42, True))
        draw.text((190, 320), "Write the prompt six times, one sample on each line. Use dark ink.",
                  fill=(75, 82, 92), font=_font(27))
        draw.text((190, 365), "Keep each letter inside its box; leave the four corner marks visible.",
                  fill=(75, 82, 92), font=_font(25))
        for cell in sheet_layout(page_id):
            x0, y0, x1, y1 = cell["cell"]
            draw.rounded_rectangle((x0, y0, x1, y1), radius=12, outline=(185, 191, 198), width=3)
            draw.text((x0 + 18, y0 + 18), cell["char"], fill=(105, 113, 122), font=_font(34, True))
            for (sx0, sy0, sx1, sy1), _ in cell["samples"]:
                # Delimit lanes outside the crop region so printed rules can
                # never be mistaken for handwritten strokes by extraction.
                draw.line((sx0, sy0 - 2, sx1, sy0 - 2), fill=(236, 239, 242), width=2)
        draw.text((190, 3210), "Scan or photograph straight-on, in even light, with the full page in frame.",
                  fill=(75, 82, 92), font=_font(24))
        pages.append((page_id, image, marks))
    return pages


def create_training_pdf(profile_home=PROFILE_HOME):
    target = Path(profile_home) / "training" / "blob-handwriting-sheets.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    pages = [item[1] for item in render_training_sheets()]
    pages[0].save(target, format="PDF", save_all=True, append_images=pages[1:], resolution=300)
    return target


def _component_markers(image):
    thumb = image.convert("L")
    if max(thumb.size) > 1600:
        scale = 1600 / max(thumb.size)
        thumb = thumb.resize((round(thumb.width * scale), round(thumb.height * scale)), Image.Resampling.BILINEAR)
    gray = np.asarray(thumb)
    mask = gray < 95
    labels, count = ndimage.label(mask, structure=np.ones((3, 3), dtype=np.uint8))
    objects = ndimage.find_objects(labels)
    stats = ndimage.sum(mask, labels, range(1, count + 1))
    h, w = gray.shape
    min_side = max(7, round(min(w, h) * .010))
    max_side = round(min(w, h) * .085)
    candidates = []
    for number, slc in enumerate(objects, 1):
        if slc is None:
            continue
        y0, y1 = slc[0].start, slc[0].stop
        x0, x1 = slc[1].start, slc[1].stop
        bw, bh = x1 - x0, y1 - y0
        if min(bw, bh) < min_side or max(bw, bh) > max_side:
            continue
        ratio = bw / max(1, bh)
        fill = float(stats[number - 1]) / (bw * bh)
        if not .72 <= ratio <= 1.38 or fill < .64:
            continue
        candidates.append(((x0 + x1) / 2, (y0 + y1) / 2, bw, bh, fill))

    corners = []
    regions = (("tl", .0, .43, .0, .37), ("tr", .57, 1., .0, .37),
               ("br", .57, 1., .63, 1.), ("bl", .0, .43, .63, 1.))
    expected = ((.045, .04), (.955, .04), (.955, .96), (.045, .96))
    for (name, xa, xb, ya, yb), (tx, ty) in zip(regions, expected):
        pool = [c for c in candidates if xa * w <= c[0] <= xb * w and ya * h <= c[1] <= yb * h]
        if not pool:
            raise ProfileBuildError("Could not find all four worksheet corner marks. Include the whole page in each photo.")
        chosen = min(pool, key=lambda c: ((c[0] / w - tx) ** 2 + (c[1] / h - ty) ** 2)
                     + .025 * abs(math.log(c[2] / c[3])))
        corners.append((chosen[0], chosen[1]))
    factor_x = image.width / w
    factor_y = image.height / h
    return [(x * factor_x, y * factor_y) for x, y in corners]


def _homography_coefficients(destination, source):
    matrix, vector = [], []
    for (x, y), (u, v) in zip(destination, source):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        vector.append(u)
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        vector.append(v)
    return np.linalg.solve(np.asarray(matrix, dtype=np.float64), np.asarray(vector, dtype=np.float64))


def rectify_photo(path):
    path = Path(path)
    try:
        size = path.stat().st_size
        if size <= 0 or size > MAX_PHOTO_BYTES:
            raise ProfileBuildError("Each handwriting photo must be under 40 MB")
        with Image.open(path) as opened:
            if opened.width * opened.height > MAX_PHOTO_PIXELS:
                raise ProfileBuildError("This photo is too large. Use a standard phone scan or smaller image.")
            image = ImageOps.exif_transpose(opened).convert("RGB")
    except ProfileBuildError:
        raise
    except (OSError, ValueError) as exc:
        raise ProfileBuildError("This photo could not be opened. Choose a JPG, PNG, or similar image.") from exc
    source_points = _component_markers(image)
    width, height = SHEET_SIZE
    inset, size = 82, 82
    destination_points = ((inset + size / 2, inset + size / 2),
                          (width - inset - size / 2, inset + size / 2),
                          (width - inset - size / 2, height - inset - size / 2),
                          (inset + size / 2, height - inset - size / 2))
    coefficients = _homography_coefficients(destination_points, source_points)
    return image.transform(SHEET_SIZE, Image.Transform.PERSPECTIVE,
                           tuple(float(v) for v in coefficients),
                           resample=Image.Resampling.BICUBIC, fillcolor=(255, 255, 255))


def _otsu(gray):
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = gray.size
    sum_total = float(np.dot(np.arange(256), hist))
    weight_bg = 0.0
    sum_bg = 0.0
    best, threshold = -1.0, 150
    for level in range(256):
        weight_bg += hist[level]
        if weight_bg <= 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg <= 0:
            break
        sum_bg += level * hist[level]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_total - sum_bg) / weight_fg
        score = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if score > best:
            best, threshold = score, level
    return threshold


def _read_sample(page, rect, baseline):
    x0, y0, x1, y1 = rect
    crop = page.crop((x0, y0, x1, y1)).convert("L")
    gray = np.asarray(crop)
    threshold = min(188, max(55, _otsu(gray)))
    ink = gray < threshold
    labels, count = ndimage.label(ink, structure=np.ones((3, 3), dtype=np.uint8))
    if count:
        sizes = ndimage.sum(ink, labels, range(1, count + 1))
        # Keep detached dots/accents while discarding scan dust.
        keep_ids = [i + 1 for i, area in enumerate(sizes) if area >= 3]
        ink &= np.isin(labels, keep_ids)
    ys, xs = np.nonzero(ink)
    if len(xs) < 12:
        return None
    pad = 4
    bx0, bx1 = max(0, int(xs.min()) - pad), min(crop.width, int(xs.max()) + pad + 1)
    by0, by1 = max(0, int(ys.min()) - pad), min(crop.height, int(ys.max()) + pad + 1)
    if bx0 <= 1 or by0 <= 1 or bx1 >= crop.width - 1 or by1 >= crop.height - 1:
        return None  # stroke touches the sample boundary: do not save a clipped glyph
    mask = Image.fromarray((ink[by0:by1, bx0:bx1] * 255).astype(np.uint8), mode="L")
    base = (baseline - y0 - by0)
    return {"image": mask, "base": float(base), "x_height": max(1.0, baseline - (y0 + int(ys.min())))}


def extract_photo(page_id, photo_path):
    """Rectify one supplied sheet and extract only handwritten glyph crops."""
    page = rectify_photo(photo_path)
    collected = {}
    empty = []
    for cell in sheet_layout(page_id):
        char, variants = cell["char"], []
        for index, (rect, baseline) in enumerate(cell["samples"], 1):
            sample = _read_sample(page, rect, baseline)
            if sample is not None:
                sample["sample"] = index
                variants.append(sample)
        if len(variants) == SAMPLES_PER_CHARACTER:
            collected[char] = variants
        else:
            empty.append(f"{char} ({len(variants)}/{SAMPLES_PER_CHARACTER})")
    return collected, empty


def build_profile(photos, profile_home=PROFILE_HOME, progress=None):
    """Build a versioned glyph bank locally; raw worksheet photos are not copied."""
    photos = {key: Path(value) for key, value in photos.items() if key in PAGE_SPECS and value}
    missing_pages = [key for key in PAGE_SPECS if key not in photos]
    if missing_pages:
        raise ProfileBuildError("Add a photo for every worksheet before creating the profile.")

    samples = {}
    for index, page_id in enumerate(PAGE_SPECS, 1):
        try:
            extracted, missing = extract_photo(page_id, photos[page_id])
        except ProfileBuildError:
            raise
        except Exception as exc:
            raise ProfileBuildError(f"Could not read the {PAGE_SPECS[page_id][0].lower()} photo: {exc}") from exc
        samples.update(extracted)
        if progress:
            progress(index, len(PAGE_SPECS), f"Read {PAGE_SPECS[page_id][0].lower()}")
        if missing:
            shown = ", ".join(missing[:8])
            more = " …" if len(missing) > 8 else ""
            raise ProfileBuildError(f"Some characters need six clear samples on {PAGE_SPECS[page_id][0]}: {shown}{more}. Retake that page in even light.")

    lower_heights = [item["x_height"] for char, variants in samples.items()
                     if char in LOWER_X_HEIGHT_CHARS for item in variants]
    if not lower_heights:
        raise ProfileBuildError("The lowercase photo did not contain readable letter samples.")
    median_xh = float(np.median(lower_heights))
    if not 8 <= median_xh <= 900:
        raise ProfileBuildError("The scans could not be calibrated. Reprint at 100% and photograph the full sheet.")
    scale = TARGET_X_HEIGHT / median_xh

    root = Path(profile_home)
    versions = root / "profiles"
    versions.mkdir(parents=True, exist_ok=True)
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    staging = versions / (".staging-" + uuid.uuid4().hex)
    final = versions / version
    staging.mkdir()
    glyph_dir = staging / "glyphs"
    glyph_dir.mkdir()
    glyph_rows = []
    try:
        for char, variants in samples.items():
            for variant_no, item in enumerate(variants, 1):
                image = item["image"]
                out_w, out_h = max(1, round(image.width * scale)), max(1, round(image.height * scale))
                image = image.resize((out_w, out_h), Image.Resampling.LANCZOS)
                base = min(float(out_h), max(0.0, item["base"] * scale))
                filename = f"u{ord(char):04x}-{variant_no}.png"
                image.save(glyph_dir / filename, optimize=True)
                glyph_rows.append({"char": char, "file": "glyphs/" + filename,
                                   "w": out_w, "h": out_h, "base": round(base, 2)})
        manifest = {"schema": PROFILE_SCHEMA, "samples_per_character": SAMPLES_PER_CHARACTER,
                    "created_utc": datetime.now(timezone.utc).isoformat(),
                    "x_height": TARGET_X_HEIGHT, "glyphs": glyph_rows,
                    "source_photos_retained": False}
        (staging / "profile.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(staging, final)
        pointer_tmp = root / (".active-" + uuid.uuid4().hex + ".tmp")
        pointer_tmp.write_text(json.dumps({"profile": version}), encoding="utf-8")
        os.replace(pointer_tmp, root / "active.json")
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"profile": version, "characters": len({row["char"] for row in glyph_rows}),
            "glyphs": len(glyph_rows), "path": final}


def load_profile(profile_home=PROFILE_HOME):
    """Load the active local profile, validating every path stays in its version folder."""
    root = Path(profile_home).resolve()
    try:
        pointer = json.loads((root / "active.json").read_text(encoding="utf-8"))
        if not isinstance(pointer, dict):
            return None
        version = pointer.get("profile", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", version):
            return None
        directory = (root / "profiles" / version).resolve()
        directory.relative_to(root / "profiles")
        manifest = json.loads((directory / "profile.json").read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or manifest.get("schema") != PROFILE_SCHEMA or
                manifest.get("samples_per_character") != SAMPLES_PER_CHARACTER):
            return None
        rows = manifest.get("glyphs", [])
        if not isinstance(rows, list):
            return None
        glyphs = {}
        for row in rows:
            if not isinstance(row, dict):
                return None
            char, relative = row.get("char"), row.get("file")
            if not isinstance(char, str) or len(char) != 1 or not isinstance(relative, str):
                continue
            image_path = (directory / relative).resolve()
            image_path.relative_to(directory)
            with Image.open(image_path) as opened:
                image = opened.convert("L")
            glyphs.setdefault(char, []).append({"image": image, "w": int(row["w"]),
                                                "h": int(row["h"]), "base": float(row["base"])})
        if set(glyphs) != REQUIRED_CHARACTERS or any(
                len(glyphs[char]) != SAMPLES_PER_CHARACTER for char in REQUIRED_CHARACTERS):
            return None
        return {"profile": version, "directory": directory, "manifest": manifest, "glyphs": glyphs}
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def has_profile(profile_home=PROFILE_HOME):
    return bool(profile_summary(profile_home))


def profile_summary(profile_home=PROFILE_HOME):
    """Read only the tiny manifest for fast startup; glyph bitmaps load on demand."""
    root = Path(profile_home).resolve()
    try:
        pointer = json.loads((root / "active.json").read_text(encoding="utf-8"))
        if not isinstance(pointer, dict):
            return None
        version = pointer.get("profile", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", version):
            return None
        directory = (root / "profiles" / version).resolve()
        directory.relative_to(root / "profiles")
        manifest = json.loads((directory / "profile.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            return None
        rows = manifest.get("glyphs", [])
        if (manifest.get("schema") != PROFILE_SCHEMA or
                manifest.get("samples_per_character") != SAMPLES_PER_CHARACTER or
                not isinstance(rows, list) or not rows or not (directory / "glyphs").is_dir()):
            return None
        valid_chars = [row["char"] for row in rows if isinstance(row, dict)
                       and isinstance(row.get("char"), str) and len(row["char"]) == 1]
        characters = set(valid_chars)
        counts = Counter(valid_chars)
        if characters != REQUIRED_CHARACTERS or any(
                counts[char] != SAMPLES_PER_CHARACTER for char in REQUIRED_CHARACTERS):
            return None
        return {"characters": len(characters), "variants": len(rows),
                "created_utc": manifest.get("created_utc", "")}
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
