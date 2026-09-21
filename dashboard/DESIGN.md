---
name: Blob Dashboard
description: Native dashboard component addendum to Blob's incumbent Liquid Glass system.
colors:
  ink-light: "#ffffff"
  ink-dark: "#000000"
---

# Design System: Blob Dashboard

## Overview

This ordinary extension inherits [Blob's Liquid Glass system](../DESIGN.md): live refractive glass, continuous corners, filled transport and restrained springs. It establishes no new identity. The parent remains read-only; the local rules below describe Dashboard components, not replacements for SmallBlob's rules. This native addendum has no web sidecar.

Sources: [dashboard.py](dashboard.py), [PRODUCT.md](PRODUCT.md), [SURFACE.md](SURFACE.md), [README.md](README.md), and the inherited font/capture implementations in `../blob.pyw`. Product capabilities and provider limitations belong in PRODUCT.md; the direction contract belongs in SURFACE.md.

Validation boundary: 13 Dashboard plus 91 base tests pass. The independent finish review resolved the reported fixes and cleared the final correction batch; this verdict covers those fixes, not the whole application or live gameplay. Saved `.impeccable/review/*.png` are synthetic GPU fixtures, not live desktop evidence. Live validation covered an earlier build only. The latest GUI was not reloaded after the user refused computer control, and is not claimed verified. Documentation review uses source only and does not launch/restart the app or operate the UI.

## Colors

**The Binary Ink Rule.** Dashboard labels, including reused Search/Playing Next labels, use full ink coverage (255 before edge antialiasing). Hierarchy comes from size and weight rather than reduced text opacity. The local shader chooses the light or dark ink token per pixel from the composited glass/artwork color: clamp sRGB, linearize at 0.04045 using the standard 12.92/2.4 transfer, then weight RGB by (0.2126, 0.7152, 0.0722). Luminance at or above 0.179 selects dark ink; below it selects light ink. There is no interpolated gray ink at the crossover.

This overrides the inherited smooth ink transition only inside `DashboardRenderer`; the shared shader string is restored after construction and the parent shader file is untouched. Temperature numerals retain adaptive ink. At 80°C a small inherited amber warning mark appears beside them; at 90°C it becomes red. Its colored radius is 2.8 DIP over a 4-DIP dark backing (RGB 20/20/20), with its center 9 DIP beyond the text bounds. This applies to System and Gaming temperatures. Backdrop and artwork provide the remaining color; synthetic fixture colors are not palette tokens.

## Typography

Observed component sizes in DIP: System temperatures 32; header/FPS 25; other Gaming values and bubble FPS 22; track title 18; section titles 16; settings and metadata 13; buttons/control labels 12; hints 11; rail labels and bubble timing 10; bubble FPS label 9. Track/section/settings titles use Semibold Text; ordinary metadata and hints use Regular. Text fitting and settings wrapping use available component width.

The implementation uses inherited Segoe UI Variable with Segoe UI fallback, including Semibold Display for large values/header, and Segoe Fluent Icons for some controls. These are observed implementation facts: system display faces and glyph icons are carried defects, not newly approved reusable tokens or prescriptions.

## Layout

Dimensions below are DIP before DPI scaling and internal supersampling. The nominal panel is 1120 × 740; actual launch fills the selected monitor's work area minus 24 in each dimension, leaving 12 at each edge. F11/header resizing uses at most 1100 × 740, bounded by that monitor's available work area. There is no invented mobile breakpoint.

Content begins at y=78, with 18 side/bottom inset, 16 inter-card gaps, and a 96-wide Gaming rail. Music receives `max(320, 0.57 × available column width)`; the remainder is the right column. System height is `min(270, 0.47 × content height)`, with Sound beneath it. Settings replaces that right column while Music remains visible.

**The Stationary Dock Rule.** The collapsed fused bubble is 92 × (92 × 98 / 104), approximately 92 × 86.69. It expands downward to 92 × 480 while retaining its top-left position. Entry places the visible dock 12 from the work-area left and 88 from its top. Both renderer coordinates use the fixed shadow padding; height changes do not bottom-anchor content. Dedicated dock buffers avoid full-overview readback.

## Elevation & Depth

Reuse the inherited GPU refraction, dispersion, rim and soft SDF shadow. Dashboard cards use strength 6, bevel 14, rim 0.55, frost 0.82 and lift 0.06; selected capsule buttons use strength 4, bevel 8, rim 0.6, frost 1 and lift 0.14. These are native shader parameters, not CSS shadows. Geometry springs use k=230, damping ratio 0.94; content crossfade uses k=190, damping ratio 1. The overview is capped at 30 Hz and the dock at 60 Hz. Artwork stays stationary; optional audio bars remain local to it.

## Shapes

The inherited 34 outer radius with an 18 inset yields 16-radius cards. Artwork is square with an 8-radius mask; capsule buttons use half-height radii. Filled previous/play/next controls use radii 20/28/20 and side centers 62 from the middle. Preserve the existing fused bubble silhouette rather than substituting a rounded rectangle.

## Components

**Keyboard controls.** Tab/Shift+Tab cycle registered targets when visible and unlocked, excluding decorative artwork/gesture regions. Focus has a persistent full-coverage inset outline independent of hover. Enter/Space activates focused non-slider controls; Search keeps text entry. Focused sliders use Left/Right steps of 0.02 and Home/End endpoints, clamped to [0,1]. Seek commits its position; Glass and sound adjustments persist through their existing handlers. F11 resizes the overview; Escape exits an unlocked dock. The dock starts click-through, with Ctrl+Alt+D as the default configurable lock chord. Its satellite expands, top chevron collapses, and Blob returns to the overview. Dashboard disables bubble dragging; it uses the system pointer.

**Isolated companion shell.** Dashboard owns `%LOCALAPPDATA%\Blob-Dashboard`, the `Local\BlobDashboard` mutex, tray identity and the `Blob Dashboard` startup registry value targeting `dashboard/app.pyw --startup`. Runtime bindings are process-local. Settings remain contextual and separate from SmallBlob. See PRODUCT.md for shared Windows media/audio effects and service limitations; isolation does not imply private audio routing.

The focus outline is implemented in `DashboardPanel.draw_focus`, shared by runtime drawing and the saved synthetic `keyboard-focus.png` fixture. A fixture is not evidence of live keyboard operation on the latest build.

**The Clean Capture Rule.** Include in screenshots freezes the captured backdrop to avoid self-refraction. Dashboard's `refresh_backdrop` briefly hides a visible panel and captures the full renderer width and maximum height, including the folded dock's hidden tail. It then freezes again and restores visibility without activation, with restoration in `finally`. Geometry resets refresh when capture inclusion is enabled and the panel is visible; dock expansion/collapse also refreshes when inclusion is enabled. Disabling inclusion restores live glass with capture exclusion. This describes implemented behavior, not a latest-build live verification claim.

## Do's and Don'ts

- Do preserve the inherited material and use this addendum only for Dashboard component differences.
- Do retain full-coverage binary contrast ink, visible keyboard focus and stationary dock expansion.
- Do keep unknown measurements explicit and production artwork source-backed; review fixtures are synthetic.
- Don't promote parent browser-roadmap prose, system display faces or glyph-icon usage into new Dashboard design rules.
- Don't infer live GUI validation from source checks, passing tests or synthetic GPU captures.

Not canonized or repaired: inherited system display faces, glyph-icon controls and parent browser-roadmap/document-format drift remain outside this documentation-only boundary. No new palette, identity or permission to propagate those defects is implied.
