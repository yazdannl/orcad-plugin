# orcad — build123d CAD tab for OrcaSlicer (v0.7)

Real Plugin-Hub plugin (Nightly / >2.4.2). Adds a top-level **orcad** tab
next to Prepare/Preview/Device/Project via `orca.pages.PagesPluginCapabilityBase`
— same mechanism as a FilamentHub-style tab. Searchable parametric objects
with **live preview**, Monaco code editor, persistent 3D preview, and
**Send to plate**; build123d runs in Orca's embedded Python, exports
STL/STEP/3MF to the plugin's `exports/` folder.

The Gridfinity Bin is a complete port of
kennetek/gridfinity-rebuilt-openscad: compartments, label tabs (all styles),
scoop, cylindrical compartments, depth/fill/height-mode controls, refined /
magnet / screw holes with crush ribs, chamfers and supportless tops,
corner-only holes, thumbscrew holes, stacking lip — verified feature by
feature against OpenSCAD-rendered reference STLs (see `verify/`).

All styling is inline (no CSS framework CDN — Orca's WebView does not
reliably load external stylesheets); only Monaco loads from CDN, with an
automatic textarea fallback, so the tab works with or without network.

## Files

- `orcad.py` — THE plugin. Single file, PEP 723, ready to upload to
  Plugin Hub or copy into `data_dir()/orca_plugins/orcad/`. The objects
  section is generated — do not edit it by hand.
- `objects/<name>.py` — one RUNNABLE build123d program per predefined object.
  Parameter variables carry `# spec:` comments that declare the UI
  (e.g. `WALL = 1.2  # spec: number label=Wall unit=mm min=0.8 max=2.4 step=0.2`).
  Never import these (they execute CAD on import); the spec is extracted
  textually by the bundler.
- `packaging/bundle.py` — inlines `objects/` into `orcad.py`
  (`--write` to regenerate, `--check` to verify; tests enforce sync).
- `tests/test_plugin.py` — pure-logic tests, run without Orca/build123d.
- `README.md`, `CHANGELOG.md`

## Manual test (you do this in Orca)

1. Use latest OrcaSlicer **Nightly** (Pages API = `main` branch; Stable 2.4.2 has no `orca.pages`).
2. Copy `orcad.py` to `<Orca data dir>/orca_plugins/orcad/orcad.py`
   (create the `orcad` folder). Or Plugins dialog → Install local plugin → pick the file.
3. Restart OrcaSlicer. Plugins dialog should list **orcad 0.7.0** with capability **orcad** (type Pages). Enable it.
4. An **orcad** tab appears in the top tab bar. Open it: left side switches between
   **Objects** (searchable dropdown, Gridfinity Bin preselected, live preview as
   you drag sliders — the Code Editor mirrors the generated code live) and **Code Editor**
   (Monaco); right side always shows the 3D preview.
5. Try: drag a Gridfinity slider → preview updates live. Press Generate + Export,
   then **⤓ Send to plate** → model should appear on Prepare (first click asks a
   one-time OS permission — allow & remember).
   Try Code Editor tab → Run + Export the gridfinity example.
6. Exports land in `.../orca_plugins/orcad/exports/`. If Send to plate does not
   load the model (association/single-instance varies by install type), drag the
   file onto Prepare instead.
7. Logs: `data_dir()/log/python_*.log` has tracebacks + `print()` output.
8. On a build without `orca.pages`, the plugin registers `orcad (needs Pages build)`
   script fallback with an upgrade message instead of a tab.

First run installs `build123d+numpy` via bundled `uv` — slow (100s of MB OCP wheel), needs network.

## Plugin Hub publish

OrcaCloud → Plugin Hub → Create listing → upload `orcad.py`,
thumbnail screenshot of CAD tab, tags (`cad`, `build123d`, `parametric`),
OS = all, compatible Orca = Nightly/>2.4.2, description + changelog from CHANGELOG.md.

## Limits (v0.7, honest)

- HTML tab only; preview is a decimated mesh render (max 3000 tris), not full CAD.
- Gridfinity Bin mirrors the original CSG tree and matches reference STLs
  (bbox/z-profiles within 0.3mm, volume within 4%, hole positions exact).
  Known approximations: thumbscrew threads → plain hole (shape differs,
  position exact); bin corner fillets r_f2 are exact on cutters; M3 threads
  in screw holes are not modeled (clearance holes only).
- Not ported (roadmap): half-grid bins, lite (hollow) bases, baseplate styles
  beyond thin+magnet (weighted/skeletonized/screw-together/fit-to-drawer),
  label-tab geometry on the baseplate, `cut_lip` for tall cylinders.
- `orca.host` exposes no plate-mutation API (verified on `main`: Plater has only
  `model` + dirty flags), so Send to plate works via OS file-open → OrcaSlicer's
  single-instance handling. Depends on file association; drag-and-drop fallback kept.
- Algebra mode only; assign final solid to `result`.
- Heavy models run in a daemon worker thread; no cancel button yet.
- License recommendation for Hub: AGPL-3.0 (Orca is AGPL-3.0).

## Verification (reproduce it)

- `verify/` holds the harness: `w_scad.py` (OpenSCAD drivers from the entry
  files), `w_b123d.py` (orcad end-to-end STL export), `w_compare.py` (STL
  metrics: bbox/volume/z-profiles/hole loops), `batch_ref.py` + `matrix.py`
  (feature matrix, 27 cases).
- Reference renders need OpenSCAD (dev snapshot ≥2023; 2021.01 cannot evaluate
  the library's `$`-scoped grid machinery) + the sources at
  `/tmp/opencode/gridfinity-rebuilt-openscad` (see Matrix paths).
- build123d runs in `.venv` (not committed). STLs land in `.verify-cache/`
  (not committed); comparison JSON is printed to stdout.
