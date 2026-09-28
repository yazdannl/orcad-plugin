# orcad OpenSCAD backend

Standard-library-only Python package used by `orcad.py`. The release bundler
also embeds it into the single-file plugin.

- `catalog.json` is the single source of truth for every object: label,
  category, icon, description, `.scad` source, and each parameter's exact
  OpenSCAD variable, type, default, range, step, unit, options, group, help,
  `depends_on` and `conflicts_with`. The frontend imports the same file at
  build time. It also holds the quality profiles (`$fa`/`$fs`: draft 12/0.8,
  balanced 6/0.3, final 3/0.1).
- `validation.py` validates parameters strictly (types, ranges, steps, options,
  cross-field rules) and reports errors as `{"fields": [...]}`.
- `runner.py` probes OpenSCAD (2023 or newer; 2021.01 is rejected) and
  renders with safe argv lists (`-D name=value`, binary STL, the Manifold
  kernel). Each render has a timeout, can be cancelled cooperatively, and runs
  with a scrubbed environment. stderr is captured into readable errors with
  line numbers. Results go into a size-bounded, content-addressed STL cache
  keyed by object/code, parameters, quality, engine version and library
  revision. Code mode sets `OPENSCADPATH` to the vendored Gridfinity tree.
- `mesh.py` validates STL output, deduplicates vertices for compact preview
  meshes, and writes 3MF.
- `bootstrap.py` installs a verified official OpenSCAD snapshot per user when
  none is on `PATH`. See the main README for the platform table and the
  newest-snapshot fallback.
- `objects/*.scad` holds the basic shapes.
- `vendor/gridfinity-rebuilt-openscad-910e22d8/` is the unmodified upstream
  Gridfinity Rebuilt source (MIT, see its `LICENSE` and `REVISION`).
