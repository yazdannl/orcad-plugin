# OpenSCAD/Gridfinity backend

This directory is the reusable backend for the React CAD frontend. The release
bundler also embeds this backend and its pinned source into single-file
`orcad.py` installs. It vendors
`kennetek/gridfinity-rebuilt-openscad` at commit
`910e22d8607fd7f5f51ad5e5cbc5287a76810bfd` under `vendor/`. The vendored `.scad`
files are unmodified. Its MIT license and Gridfinity attribution are retained
in `vendor/gridfinity-rebuilt-openscad-910e22d8/LICENSE` and `README.md`.

## Dependency and compatibility

The plugin starts a background per-user bootstrap when the Pages capability is
registered. It first reuses a supported system executable; when none is
available, it downloads a pinned official OpenSCAD snapshot, verifies its
SHA-256 digest, and stores it without sudo/admin privileges under the platform's
user cache (`~/.cache/orcad/openscad`, `~/Library/Caches/orcad/openscad`, or
`%LOCALAPPDATA%/orcad/openscad`). The pinned artifact table is in
`bootstrap.py`; it currently covers Linux x86_64/ARM64 AppImages, the universal
macOS disk image, and the Windows x86_64 ZIP. Unsupported platforms receive a
manual-install error rather than an unverified download. The download needs
network and write access and the first startup can take time.

The upstream project recommends development snapshots for render performance;
this backend requires a version at or newer than the current upstream
compatibility floor (2023-era builds). **OpenSCAD 2021.01 is explicitly
rejected**: it cannot reliably evaluate the `$`-scoped grid machinery used by
this source. `probe_openscad()` returns a flagged `EngineInfo` and rendering
returns the stable `openscad_unsupported_version` error instead of attempting a
known-incompatible render.

`compatibility_smoke_test()` is an opt-in hook for checking a local executable
against a small catalog entry. It does not rewrite source files.

## Catalog and quality

`catalog.json` is canonical for the two entry files, labels, types, defaults,
ranges, steps, groups, dependencies/conflicts, and exact OpenSCAD variable
names. `$fa` and `$fs` are intentionally not user parameters. They are part of
one of the explicit runner profiles:

| profile | `$fa` | `$fs` | intent |
| --- | ---: | ---: | --- |
| `draft` | 12 | 0.8 | quick interaction preview |
| `balanced` | 6 | 0.3 | normal preview/export |
| `final` | 3 | 0.1 | higher tessellation export |

These are quality settings, not render-time guarantees. Every result exposes
`duration_ms`; no universal sub-one-second render claim is made. Interactive
callers can pass `cache_dir` to `OpenSCADRunner` for content-addressed STL
reuse; the plugin uses `.openscad-cache/` beside its entry point and ignores
cache-write failures.

## Runner contract

Use `validate_parameters()` before crossing the process boundary. `build_argv`
constructs an argv list with `-D name=value`, explicitly requests `binstl`, and
never builds a shell command or rewrites source. `OpenSCADRunner.render()` uses
a temporary STL beside the requested output, atomically moves a successful
result into place, validates binary STL output, and returns a JSON-serializable
`RenderResult`. A valid cache hit copies the checked mesh without spawning
OpenSCAD. The optional cancellation callback/event, `cancel()`, and
`render_latest()` support a host that discards stale requests.

`stl.py` provides a compact deduplicated vertex/index mesh, binary STL parsing
and encoding, bbox/count statistics, and volume only when the mesh has closed
edges. `cache_key()` includes source revision, engine version, object,
normalized parameters, and quality profile/settings.
