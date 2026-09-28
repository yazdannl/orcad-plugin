# Changelog

## 0.9.0 — Integrated AI OpenSCAD assistant

- Added an optional Pi coding agent to Code mode with streaming chat, thinking,
  tool activity, code updates, revert, Stop, and New chat.
- Added SHA-256-pinned Node.js 24.21.0 provisioning and exact Pi 0.87.1 npm
  installation in a private per-user cache; existing Pi login or an orcad-owned
  provider key can be used.
- Restricted the agent to `model.scad` file tools and added a local Manifold
  OpenSCAD render tool that reports diagnostics and model bounds.
- Added worker-thread setup/configuration, progress, cancellation, and RPC
  lifecycle handling. Prompts and source are sent to the selected AI provider;
  renders remain local.

## 0.8.0 — OpenSCAD-only overhaul

- **One CAD engine.** Every model is rendered by OpenSCAD. build123d, OCP and
  NumPy are gone from the plugin's dependencies, so installing it no longer
  pulls hundreds of MB of wheels and can no longer fail because no OCP wheel
  exists for OrcaSlicer's Python. Box, Cylinder, Tube and Mounting plate were
  ported to `.scad`. Code mode is now an OpenSCAD editor that can
  `include <src/...>` the bundled Gridfinity library.
- **Fast renders.** Uses the Manifold kernel (about 50x faster than CGAL for
  Gridfinity) plus a size-bounded, content-addressed STL cache. Exporting after
  a preview is instant.
- **Reliable setup.** The Linux AppImage is extracted, so FUSE is no longer
  needed. If the pinned snapshot is deleted upstream, the newest one is
  verified against its published digest. The header reports download progress,
  failed setups are retried on the next render, and the previous install is
  reused without touching the network.
- **Readable errors.** OpenSCAD's stderr is captured. Syntax errors report the
  line (highlighted in the editor), 2D or empty results are explained, and the
  full output is shown in a console. Validation errors point at the exact
  parameters, both client-side and server-side.
- **Send to plate** launches the running OrcaSlicer with `--single-instance`
  (`open -a` on macOS) instead of relying on the OS file association.
- **3MF export** for every model, alongside STL.
- **New UI.** Gradient styling that follows OrcaSlicer's light/dark theme, and
  a responsive layout (three columns, two columns, then a single stacked
  column). Adds object cards, grouped parameters, sliders with number fields,
  switches, segmented options, orbit/pan/zoom with standard views, wireframe,
  edge and plate toggles, and toasts.
- **Fixes:** a WebGL failure no longer blanks the page (software fallback).
  React no longer wipes the canvas when a notice appears. Host-injected
  element CSS can no longer restyle the controls, which also let Tailwind go.
  Parameters are kept per object. Previews are debounced, and the newest
  preview wins. Windows renders no longer flash a console window. The host's
  `LD_LIBRARY_PATH` and similar variables no longer leak into OpenSCAD. The
  backend is loaded under a private module name so it cannot collide with
  other plugins.
- **Smaller, stricter protocol.** `hello` / `engine` / `render` / `cancel` /
  `open_exports` messages. Meshes travel as base64 float32/uint16 buffers
  instead of JSON number lists (several times smaller).
- **Dev server** (`dev/serve.py`) that simulates the host for browser work,
  and a simpler deterministic bundler. The build123d geometry-verification
  harness and compatibility matrix were removed along with the engine.

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
- Added a silent startup bootstrap for missing or too-old OpenSCAD: it reuses a
  supported system executable or downloads a pinned official per-user artifact,
  verifies its SHA-256 digest, and avoids sudo/admin package-manager actions.
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
