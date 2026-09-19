# Changelog

## 0.6.0 — Object files are build123d programs

- Each `objects/<name>.py` is now a real runnable build123d program; the UI
  spec is extracted from `# spec:` comments on its parameter variables
  (e.g. `WALL = 1.2  # spec: number label=Wall unit=mm min=0.8 max=2.4 step=0.2`).
- The bundler also generates the dropdown spec block in the page, so objects,
  editor code, params and UI all come from the single file — zero drift.
- Param values are baked into the program's own variable lines; the Code
  Editor shows the actual object file with your values in it.

## 0.5.1 — Gridfinity verified against original .scad

- Bin + baseplate rewritten as a faithful port of
  kennetek/gridfinity-rebuilt-openscad (file:line citations inline).
  Fixed real bugs found by STL-vs-STL comparison: off-center cells,
  flat-bottom slab, buried single magnet hole, box sockets, square-only lip.
- Verified: OpenSCAD-rendered reference STLs vs build123d output match
  (footprint, foot taper, magnet positions within 0.3mm). Known deltas:
  nominal 4.4 lip, ~6% volume (lip + omitted interior fillets).
- Predefined objects now live in `objects/*.py` (one file each), bundled
  into `orcad.py` via `packaging/bundle.py` (`--check` enforced by tests).

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
