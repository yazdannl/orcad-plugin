# Changelog

## 0.5.0 — Editor mirror + Rebuilt-style Gridfinity port

- Code Editor mirrors the Objects tab: selecting an object or moving any
  slider instantly rewrites the editor code (pure codegen `code` command, no
  CAD run); invalid intermediate states keep the last good code, hand edits
  are never clobbered except by your own object/param changes.
- Gridfinity Bin ported to Rebuilt-style geometry: lofted tapered stacking
  feet with true 45° chamfers (was: stepped boxes), tapered stacking lip ring
  that actually nests the feet above (was: straight frame that blocked them),
  new divider walls (DX/DY), front scoop notch. Magnet holes unchanged.
- Gridfinity Bin preselected on open; editor opens showing its code.

## 0.4.0 — Live preview + Send to plate

- Live preview for Objects: debounced rebuild on slider/param change (650ms),
  export-free, with seq guard dropping stale results; errors shown subtly in
  the preview header without clearing the last good mesh. Tab opens already
  rendering the default Gridfinity Bin.
- Send to plate: exports STL and opens it with the OS default app so
  OrcaSlicer's single-instance handling loads it onto the build plate
  (verified: no plate-mutation API exists in `orca.host` on `main` — same
  mechanism generator plugins use). One audit prompt on first use, then
  remembered; drag-and-drop fallback kept and documented.

## 0.3.0 — Self-contained modern UI + Gridfinity

- UI reworked: 100% inline CSS (no framework CDN — Orca's WebView
  does not reliably load external stylesheets), dark-first theme adapting via
  `--orca-*` vars; only Monaco still uses CDN, with textarea fallback.
- Objects tab: searchable predefined-object dropdown (Box, Cylinder, Tube,
  Bracket, Gridfinity Bin, Gridfinity Baseplate) + styled sliders/checkboxes.
- New: spec-based Gridfinity Bin (42mm grid, 7mm units, stepped stacking foot,
  optional lip + 6x2 magnet holes) and Gridfinity Baseplate (sockets + magnets).
- Stateful defaults: Gridfinity Bin preselected; Gridfinity example in editor.
- int/bool parameter types with validation.

## 0.2.0 — Modern UI + always-on preview

- Bootstrap 5 UI (jsdelivr CDN, Orca `--orca-*` theme mapping, offline fallback).
- Left panel switches between Primitives and Code Editor tabs; single Run + Export acts on the active tab.
- Monaco code editor with plain-textarea fallback when the CDN is unreachable.
- Persistent 3D preview (dependency-free canvas renderer: orbit/zoom/spin/wireframe) fed by a decimated server-side tessellation payload; stats-only fallback.
- Exports unchanged (STL/STEP/3MF to plugin-local `exports/`).

## 0.1.0 — MVP

- Top-level CAD tab via `orca.pages.PagesPluginCapabilityBase` (FilamentHub-style).
- Parametric primitives: Box, Cylinder, Tube, Bracket-with-holes (sliders + validation).
- Code editor + 3 built-in examples, `result = ...` convention.
- Export STL (`export_stl`), STEP (`export_step`), 3MF (`Mesher`) to plugin-local `exports/`.
- Worker-thread exec, progress + result/error posts, audit-friendly paths.
- Script-capability fallback with upgrade message on builds lacking `orca.pages`.
