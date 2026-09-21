# orcad — build123d CAD tab for OrcaSlicer (v0.7)

Real Plugin-Hub plugin (Nightly / >2.4.2). Adds a top-level **orcad** tab
next to Prepare/Preview/Device/Project via `orca.pages.PagesPluginCapabilityBase`
— same mechanism as a FilamentHub-style tab. Searchable parametric objects
with **live preview**, a compact native code editor, Three.js 3D preview, and
**Send to plate**; build123d runs in Orca's embedded Python, exports
STL/STEP/3MF to the plugin's `exports/` folder.

The Gridfinity Bin is a complete port of
kennetek/gridfinity-rebuilt-openscad: compartments, label tabs (all styles),
scoop, cylindrical compartments, depth/fill/height-mode controls, refined /
magnet / screw holes with crush ribs, chamfers and supportless tops,
corner-only holes, thumbscrew holes, stacking lip — verified feature by
feature against OpenSCAD-rendered reference STLs (see `verify/`).

The UI is a compact local Vue app with Tailwind CSS and a bundled Three.js
runtime. `frontend/dist/index.html` is self-contained and loaded beside
`orcad.py` when present; `orcad.py` also carries a compressed fallback copy so
single-file installs still work. End users need no Node.js or CDN to load the
UI. The first CAD run may still need network and write access to install the
Python dependencies.

## Files

- `orcad.py` — plugin entry point. It loads the compiled frontend beside it
  when available and includes a compressed fallback for single-file installs.
- `frontend/src/` — Vue/Tailwind source.
- `frontend/dist/index.html` — compiled, self-contained frontend artifact.
  Build it with `cd frontend && npm ci && npm run build`; Node is needed only
  by contributors, never by plugin users.
- `objects/<name>.py` — one RUNNABLE build123d program per predefined object.
  Parameter variables carry `# spec:` comments that declare the UI
  (e.g. `WALL = 1.2  # spec: number label=Wall unit=mm min=0.8 max=2.4 step=0.2`).
  Never import these (they execute CAD on import); the spec is extracted
  textually by the bundler.
- `packaging/bundle.py` — inlines `objects/` into `orcad.py` and keeps the
  frontend parameter spec in sync (`--write` to regenerate, `--check` to verify;
  tests enforce sync).
- `tests/test_plugin.py` — pure-logic tests, run without Orca/build123d.
- `tests/test_geometry_integration.py` — optional build123d/OCP regression suite for
  default solids, cross-sections, and export success/failure paths.
- `verify/geometry.py` — bounded integration-test entry point using `.venv`.
- `README.md`, `CHANGELOG.md`

## Manual test (you do this in Orca)

1. Use latest OrcaSlicer **Nightly** (Pages API = `main` branch; Stable 2.4.2 has no `orca.pages`).
2. Copy `orcad.py` to `<Orca data dir>/orca_plugins/orcad/orcad.py`
   (the embedded frontend fallback makes the Python file sufficient). If
   developing locally, you may also copy `frontend/dist/index.html` beside it.
3. Restart OrcaSlicer. Plugins dialog should list **orcad 0.7.0** with capability **orcad** (type Pages). Enable it.
4. An **orcad** tab appears in the top tab bar. Open it: left side switches between
   **Objects** (filterable model list, Gridfinity Bin preselected, live preview as
   you drag sliders) and **Code** (native build123d editor); right side always
   shows the Three.js preview.
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

## First-run setup and readiness

The first CAD preview or export may install `build123d` and `numpy` (including the
OCP dependency) through Orca's bundled `uv`. This can download hundreds of MB,
needs network and write access, and may take time. The UI reports **Bridge** and
**CAD/model** status separately: bridge ready means only that the host messaging
API is available; CAD/model readiness is checked by a preview or export. There is
no separate dependency probe, so the plugin does not claim CAD is ready before a
real operation succeeds.

If setup or a build fails, check network and write permissions, reopen the Plugins
dialog or restart OrcaSlicer to retry dependency setup, then run again. Read the
error and `data_dir()/log/python_*.log` if it still fails. A failed operation does
not replace the last successful preview or export.

## Code trust and recovery

Editable Python/build123d code is **trusted** and executes in-process with
**no security sandbox**. Any import checks used while validating predefined objects
are validation/UX only, not a security boundary. Do not run code you do not
trust. Assign the final solid to `result`, for example:

```python
from build123d import *
result = Box(20, 20, 20)
```

For a code error, fix the reported error and press **Run** again; the last
successful result remains unchanged. A missing `result`, a flat sketch, or a
non-solid can also prevent export.

## Plugin Hub publish

OrcaCloud → Plugin Hub → Create listing → upload `orcad.py` (or the package
with `frontend/dist/index.html`), thumbnail screenshot of CAD tab, tags
(`cad`, `build123d`, `parametric`), OS = all, compatible Orca = Nightly/>2.4.2,
description + changelog from CHANGELOG.md.

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
- Busy operations report queued/building/tessellating/exporting/handoff stages and elapsed time. Cancel removes pending work; running build123d/native CAD work is not hard-stopped safely, so it finishes in the worker and its result is discarded.
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
- Run the optional geometry regressions with `python3 verify/geometry.py`. The
  harness uses `.venv` for build123d/OCP, keeps exports in pytest temporary
  directories, and skips with install instructions when the optional dependency
  is unavailable. Pure tests remain independent: `python3 -m pytest tests/ -q`.
