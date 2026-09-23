# Changelog

## 0.7.0 — Supported release and parity notes (Supported versions)

- 0.7.0 is the single plugin release version. `compatibility.json` is the
  version authority; `orcad.py`, frontend package metadata, and this release
  documentation are synchronized to it.
- Declared the tested Python, build123d, OCP, NumPy, Node.js, npm, and pytest
  constraints, the external OpenSCAD `>=2023.0` requirement for Gridfinity,
  the Nightly `main` Pages API target, and the stable 2.4.2 limitation.
- Replaced the page with a React + Tailwind app and committed the self-contained
  `frontend/dist/index.html` artifact. Node is needed only to rebuild it, not to
  run the plugin.
- Added a direct Three.js WebGL viewport with indexed binary-STL decoding,
  demand-driven rendering, disposal, wireframe/spin controls, and latest-only
  request handling. The page has no runtime CDN dependency.
- Added the reusable vendored OpenSCAD/Gridfinity backend. Bin/Baseplate
  object-mode requests use the pinned upstream entry files, strict catalog
  validation, safe argv lists, draft/balanced/final quality profiles,
  cancellation, timeout handling, and an opt-in content-addressed STL cache.
  The release bundler embeds the backend and pinned `.scad` source so the
  single-file `orcad.py` install retains Gridfinity support.
- Preserved build123d for simple predefined objects and trusted Code mode;
  upstream object mode currently exports STL only. The complete upstream mesh
  is transported for preview rather than being silently decimated.
- Added AGPL-3.0-only licensing for original project code, separate React,
  Three.js, Tailwind, and build-tool notices, and retained the upstream
  Gridfinity MIT license and attribution unchanged.

## 0.6.0 — Object files are build123d programs

- Each `objects/<name>.py` is now a real runnable build123d program; the UI
  spec is extracted from `# spec:` comments on its parameter variables
  (e.g. `WALL = 1.2  # spec: number label=Wall unit=mm min=0.8 max=2.4 step=0.2`).
- The bundler also generates the dropdown spec block in the page, so objects,
  editor code, params and UI all come from the single file — zero drift.
- Param values are baked into the program's own variable lines; the Code
  Editor shows the actual object file with your values in it.

## 0.5.1 — Gridfinity comparison harness

- Bin + baseplate were rewritten as ports of
  kennetek/gridfinity-rebuilt-openscad (file:line citations inline).
  Fixed geometry deltas found by STL comparison: off-center cells,
  flat-bottom slab, buried single magnet hole, box sockets, and square-only
  lip.
- The recorded OpenSCAD comparison covered footprint, foot taper, and magnet
  positions within the stated tolerances. It also recorded known deltas,
  including the nominal 4.4 lip and volume differences from omitted interior
  fillets. This remains approximate parity, not an upstream certification.
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
- Send to plate: exports STL and requests the OS default app so
  OrcaSlicer's single-instance handling may load it onto the build plate.
  The host has no plate-mutation API on `main`; file association and host
  behavior are therefore experimental/host-dependent. One audit prompt on
  first use, then remembered; drag-and-drop fallback kept and documented.

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
