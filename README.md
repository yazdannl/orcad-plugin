# OrcaCAD — build123d CAD tab for OrcaSlicer (v0.3)

Real Plugin-Hub plugin (Nightly / >2.4.2). Adds a top-level **CAD** tab
next to Prepare/Preview/Device/Project via `orca.pages.PagesPluginCapabilityBase`
— same mechanism as a FilamentHub-style tab. Searchable parametric objects
(incl. spec-based **Gridfinity bins + baseplates**), Monaco code editor,
persistent 3D preview; build123d runs in Orca's embedded Python, exports
STL/STEP/3MF to the plugin's `exports/` folder.

All styling is inline (no CSS framework CDN — Orca's WebView does not
reliably load external stylesheets); only Monaco loads from CDN, with an
automatic textarea fallback, so the tab works with or without network.

## Files

- `orcacad_plugin.py` — THE plugin. Single file, PEP 723, ready to upload to
  Plugin Hub or copy into `data_dir()/orca_plugins/OrcaCAD/`.
- `tests/test_plugin.py` — pure-logic tests, run without Orca/build123d.
- `README.md`, `CHANGELOG.md`

## Manual test (you do this in Orca)

1. Use latest OrcaSlicer **Nightly** (Pages API = `main` branch; Stable 2.4.2 has no `orca.pages`).
2. Copy `orcacad_plugin.py` to `<Orca data dir>/orca_plugins/OrcaCAD/orcacad_plugin.py`
   (create the `OrcaCAD` folder). Or Plugins dialog → Install local plugin → pick the file.
3. Restart OrcaSlicer. Plugins dialog should list **OrcaCAD 0.3.0** with capability **CAD** (type Pages). Enable it.
4. A **CAD** tab appears in the top tab bar. Open it: left side switches between
   **Objects** (searchable dropdown, Gridfinity Bin preselected) and **Code Editor**
   (Monaco); right side always shows the 3D preview.
5. Try: Generate + Export the Gridfinity Bin → expect Result card + 3D preview.
   Try Code Editor tab → Run + Export the gridfinity example.
6. Exports land in `.../orca_plugins/OrcaCAD/exports/`. Drag one onto Prepare → it loads as a model.
7. Logs: `data_dir()/log/python_*.log` has tracebacks + `print()` output.
8. On a build without `orca.pages`, the plugin registers `OrcaCAD (needs Pages build)`
   script fallback with an upgrade message instead of a tab.

First run installs `build123d+numpy` via bundled `uv` — slow (100s of MB OCP wheel), needs network.

## Plugin Hub publish

OrcaCloud → Plugin Hub → Create listing → upload `orcacad_plugin.py`,
thumbnail screenshot of CAD tab, tags (`cad`, `build123d`, `parametric`),
OS = all, compatible Orca = Nightly/>2.4.2, description + changelog from CHANGELOG.md.

## Limits (v0.3, honest)

- HTML tab only; preview is a decimated mesh render (max 3000 tris), not full CAD.
- Gridfinity uses a simplified stepped foot/lip profile (spec dims: 42mm grid,
  7mm units, 0.5 tolerance, 26mm magnet grid) — seats in standard baseplates,
  not a micron-exact clone of gridfinity-rebuilt.
- `orca.host` is read-only → **manual drag-to-import**. No auto-push to plater.
- Algebra mode only; assign final solid to `result`.
- Heavy models run in a daemon worker thread; no cancel button yet.
- License recommendation for Hub: AGPL-3.0 (Orca is AGPL-3.0).
