<img src="assets/orcad-logo.png" width="96" align="right" alt="OrcaCAD logo">

# OrcaCAD: parametric CAD tab for OrcaSlicer (v0.9.15)

OrcaCAD adds an **OrcaCAD** tab next to Prepare / Preview / Device in OrcaSlicer. It
lets you pick a parametric model, tune it with live 3D preview, and put it on
the build plate with one click.

- **Library:** 49 parametric models in six categories - Gridfinity (19),
  Organization, Cases, Fasteners, Panels and Basics. Gridfinity comes from
  pinned upstream [Gridfinity Rebuilt](https://github.com/kennetek/gridfinity-rebuilt-openscad)
  source, the rack models from [rackstack](https://github.com/jazwa/rackstack),
  the split-flap parts from [splitflap](https://github.com/scottbez1/splitflap)
  and the metric fasteners from [threads-scad](https://github.com/rcolyer/threads-scad),
  next to a Box, Cylinder, Tube and Mounting plate. Every parameter gets a
  slider, number field, switch or option picker, with inline validation, and
  many objects ship **presets** - one click for a sensible starting point. Each
  card shows the rendered preview as a square header image, then the short name,
  the category and a short description; the list scrolls in a region a little
  over two card rows tall, and dragging the divider between the library and the
  viewport widens the sidebar: the cards then flow into as many columns as fit,
  so a wide sidebar shows two or three times as many models at once, and the
  width is remembered. The row cut in half is the cue that there is more below.
- **Code:** an OpenSCAD editor with examples, line numbers, error-line
  highlighting and Ctrl+Enter to render. `include <src/...>` loads the bundled
  Gridfinity library, and every other vendored library is on `OPENSCADPATH`
  too, so you can build your own bins, racks and split-flap parts.
- **Preview:** Three.js viewport with orbit/pan/zoom, iso/front/right/top views,
  wireframe, edge and build-plate toggles. It falls back to a software renderer
  when the webview has no WebGL.
- **Output:** export **STL** or **3MF**, or **Send to plate**, which hands the
  STL to the running OrcaSlicer. The side panel shows dimensions, volume,
  triangle count, render time, recent exports and the OpenSCAD console.
- **UI:** gradient styling that follows OrcaSlicer's light/dark theme. The
  layout adapts from wide desktop windows (three columns) through laptops
  (two columns) down to narrow windows (a single stacked column).
- **AI assistant:** an optional integrated Pi agent in Code mode. It edits the
  current `model.scad`, renders locally with OpenSCAD, and iterates on errors.

## Model catalog

Forty-nine models ship in `openscad/catalog.json`, which is the single source
of truth for the UI, the validation and the renderer:

| Category | Models | What they cover |
| --- | --- | --- |
| Gridfinity | 19 | Bins, baseplates, blocks, pockets, baskets, a chess set, glue stick, silverware, socket holder |
| Organization | 8 | Battery holder, PCB mount, cable clip, device stand, wall hook, bit holder, drawer divider, wall plate |
| Cases | 8 | Rack trays, patch panel, ventilated plate, enclosed box, angle bracket, fan tray, brush strip, SBC case |
| Fasteners | 6 | Metric hex bolts, nuts, countersunk bolts and wood screws, a threaded rod, a hole coupon |
| Panels | 4 | Split-flap card, spool, scoring jig, punch jig |
| Basics | 4 | Box, cylinder, tube, bracket |

- **Parameters** carry their exact OpenSCAD variable, type, default, range,
  step, unit, group and help text. The renderer passes them as `-D name=value`,
  so what the slider shows is what OpenSCAD gets.
- **Presets** are named parameter sets per object ("Desk panel (2x8)",
  "Stack of eight"). Picking one applies it on top of the current values and
  the header shows which preset is active; a manual edit just clears that
  marker. A preset can never land on an illegal combination of options.
- **Tags** are the extra search terms the catalog search matches, so "m3",
  "gridfinity" or "rack" finds a model without knowing its name.

Models come from pinned upstream sources; the exact commit, license and file
list for each of the seven vendored libraries are in `NOTICE`,
`THIRD_PARTY_NOTICES` and `openscad/README.md`. No model source is fetched at
render time.

## How it works

Every model is rendered by **OpenSCAD** with its fast Manifold kernel, so a
typical Gridfinity bin takes well under a second. The plugin has **no Python
dependencies** (`dependencies = []`). OpenSCAD is provisioned on first use if
needed; the AI runtime is optional and downloads only when you choose **Install
pi** in the Code view.

On first start the plugin looks for OpenSCAD 2023 or newer on `PATH`. If it
finds none, it downloads an official OpenSCAD development snapshot (about 80 MB)
into OrcaCAD's cache folder, verifies it against a pinned SHA-256 digest, and
never asks for admin rights:

| Platform | Artifact |
| --- | --- |
| Linux x86_64 / ARM64 | AppImage, extracted, so FUSE is not required |
| macOS (Intel + Apple Silicon) | universal `.dmg` |
| Windows x86_64 | portable `.zip` |

That cache folder and the rest of OrcaCAD's runtime data live in
`<Orca data dir>/orcad`, which OrcaSlicer's plugin sandbox pre-approves, so it
never asks you for filesystem permission. Loaded outside OrcaSlicer (development,
`dev/serve.py`) the per-user cache is used instead: `%LOCALAPPDATA%\orcad`,
`~/Library/Caches/orcad` or `~/.cache/orcad`.

Upstream eventually deletes old snapshots. When the pinned file is gone, the
plugin picks the newest snapshot for the platform and verifies it against the
digest published beside it. The header shows the setup progress. The stable
OpenSCAD 2021.01 release is too old for the Gridfinity library and is ignored.

### AI assistant: setup, authentication, and privacy

On request, **Install pi** provisions official Node.js **24.21.0** (SHA-256
pinned per supported platform) and installs `@earendil-works/pi-coding-agent`
**0.87.1** with that runtime's npm. The Node archive is about **27–38 MB**
compressed depending on platform; the Pi package tarball is about **7.3 MB**
(23 MB unpacked), plus its npm dependencies. No admin rights are required.

The runtime, `model.scad` workspace, OrcaCAD-managed Pi agent directory, and
custom endpoint key files live under `<Orca data dir>/orcad/ai` (or
`%LOCALAPPDATA%\orcad/ai`, `~/Library/Caches/orcad/ai` or
`$XDG_CACHE_HOME/orcad/ai` outside OrcaSlicer).

In Code → Settings, choose **Use my pi setup** to reuse `~/.pi/agent`, or
**OrcaCAD-managed** for a separate account/configuration under the cache above.
Managed setup supports Pi's built-in browser/device sign-in and API-key login
(e.g. GitHub Copilot, Anthropic Claude Pro/Max, and OpenAI ChatGPT), plus Sign
out. Pi itself stores those credentials in
`<Orca data dir>/orcad/ai/private-agent/auth.json`; OrcaCAD never returns the saved values to the
page or writes them to chat/history. One-time authorization links/device codes
and the prompts needed to complete sign-in appear only in a transient dialog.

Use **Custom endpoint** for Ollama, LM Studio, vLLM, and other compatible
servers. Enter its base URL, API type (OpenAI Completions/Responses, Anthropic
Messages, or Google Generative AI), and model IDs, or use **Detect models** to
query `{baseUrl}/models`. The managed `models.json` contains only endpoint
settings and an environment-variable reference; an optional endpoint key is
stored separately in a mode-`0600` file beneath the private agent directory.
Keyless/local endpoints use a placeholder key when required by Pi.

The AI subprocess is restricted to `read`, `edit`, and `write` for `model.scad`,
plus OrcaCAD's local OpenSCAD render tool; it has no shell tool. **Privacy:**
prompts and the current OpenSCAD source are sent to the selected model provider
for inference. OpenSCAD rendering itself runs locally. Do not send designs you
are not comfortable sharing with that provider. AI-generated code can be unsafe;
inspect it before using or printing it.

**Send to plate** writes an STL to `exports/` beside the plugin. On Windows,
OrcaCAD's render worker finds the current-process `wxWindowNR` main frame carrying
Orca's `Instance_Hash_Minor` and `Instance_Hash_Major` properties, then sends
`WM_COPYDATA` (`dwData=1`) with Orca's semicolon-separated, C-style escaped
argv encoded as a NUL-terminated UTF-16 string. The receiver narrows it to
UTF-8, skips argv[0], and queues existing file paths for `Plater::load_files()`.
`SendMessageW` is synchronous, so this runs on the render worker—not from page
`on_message` on the UI thread. The receiver's return only confirms message
handling, not that import finished; check Prepare.

If Windows IPC is unavailable, the fallback launches `OrcaSlicer.exe <file>`
without `--single-instance`. Orca's CLI parser returns `CLI_INVALID_PARAMS`
(-2) for the `--single-instance` form before GUI startup; normal positional file
arguments are forwarded according to Orca's single-instance preference. The
Linux executable fallback likewise passes the file positionally; macOS keeps
`open -a OrcaSlicer.app <file>`. This IPC pattern is implemented by the public
[Gridfinity generator](https://github.com/JonasMerrell/Gridfinity-Orca-Plugin/blob/71090b9121598de3477ea7ae8cd34522d41bdcf8/build_orca_plugin.py#L333-L382)
and [Model Search plugin](https://github.com/tommasobbianchi/OrcaSlicer-Model-Search-Plugin/blob/576821fc0ef9a812088d7576bb678d8ec1f3a884/search_engine.py#L123-L185).
The supported Python host API does not bind `Plater.load_files()`. See Orca's
[CLI argument parser](https://github.com/OrcaSlicer/OrcaSlicer/blob/46fb5126903578e2b32f1a3caa6cb848370a496a/src/OrcaSlicer.cpp#L7977-L8013) and [the -2 return](https://github.com/OrcaSlicer/OrcaSlicer/blob/46fb5126903578e2b32f1a3caa6cb848370a496a/src/OrcaSlicer.cpp#L1350-L1354),
[GUI instance setup](https://github.com/OrcaSlicer/OrcaSlicer/blob/46fb5126903578e2b32f1a3caa6cb848370a496a/src/slic3r/GUI/GUI_Init.cpp#L43-L50),
[instance serialization and decoding](https://github.com/OrcaSlicer/OrcaSlicer/blob/46fb5126903578e2b32f1a3caa6cb848370a496a/src/slic3r/GUI/InstanceCheck.cpp#L65-L145),
[Windows message receiver](https://github.com/OrcaSlicer/OrcaSlicer/blob/46fb5126903578e2b32f1a3caa6cb848370a496a/src/slic3r/GUI/GUI_App.cpp#L700-L707),
and [plate event handler](https://github.com/OrcaSlicer/OrcaSlicer/blob/46fb5126903578e2b32f1a3caa6cb848370a496a/src/slic3r/GUI/Plater.cpp#L8119-L8128). Use **Copy path** or **Open folder** and drag the STL onto Prepare if it is still missing.

## Install

Requires an OrcaSlicer build with plugin pages (`orca.pages`, the current
Nightly). Builds without it get an "OrcaCAD (needs a newer OrcaSlicer)" entry
instead of a tab.

- **Release download (recommended):** download `orcad.py` from the
  [latest release](https://github.com/yazdannl/orcad-plugin/releases/latest), then
  in OrcaSlicer open **Plugins**, use the arrow next to **Browse plugins** and
  choose **Install local plugin**, and pick that file. OrcaSlicer installs and
  enables it for you; every later release is installed the same way.
- **Orca Cloud:** published in the Plugin Hub at
  [cloud.orcaslicer.com](https://cloud.orcaslicer.com). Sign in with the same
  account in OrcaSlicer, subscribe, then open **Plugins → Refresh** and tick
  **Activate**. Updates arrive from each new release. Loading cloud plugins is
  currently a Beta feature of the Nightly builds.
- **Single file (manual):** `orcad.py` embeds the compiled page, OpenSCAD
  backend/Gridfinity sources, and AI backend code. Put it in
  `<Orca data dir>/orca_plugins/orcad/orcad.py`.
- **Folder:** `orcad.py` plus `frontend/dist/index.html`, `openscad/`, and `ai/`
  in the same folder. Files beside `orcad.py` take precedence over embedded
  copies.

Restart OrcaSlicer and enable **OrcaCAD** in the Plugins dialog. Runtime data
lives next to the plugin: `exports/` (your files) and `.cache/` (rendered STL
cache, capped at 512 MB). For a single-file install the OpenSCAD backend is
unpacked into the cache folder below `orcad.py` instead of beside it
(`<Orca data dir>/orcad/backend`), because a plugin directory installed from
Orca Cloud is already a long path. The AI cache location is listed above.

## Troubleshooting

- **OrcaSlicer asks OrcaCAD to create files** (the dialog reads *Plugin "OrcaCAD"
  is requesting to create the following file(s)*): 0.9.11 and older kept their
  cache outside OrcaSlicer's own data directory (`%LOCALAPPDATA%\orcad` and
  siblings), so the plugin sandbox asked for permission - again on every start,
  because OrcaSlicer does not remember directory-creation grants, and when the
  question was asked from a worker thread the plugin's load failed no matter
  which answer you picked. 0.9.12 moves the cache to `<Orca data dir>/orcad` and
  the question is gone. Update the plugin (or install the current `orcad.py` from
  the release page); no cleanup of the old folder is needed.
- **"Setting up OpenSCAD" never finishes or fails:** the first run needs
  network access to `files.openscad.org`. Hover or click the red pill for the
  reason. As an alternative, install an OpenSCAD 2023+ development snapshot
  and put it on `PATH`.
- **AI setup fails:** Install pi needs network access to `nodejs.org` and the
  npm registry. Retry from the AI setup card; Node and Pi versions are pinned.
- **A render fails:** the message appears over the viewport; the Library
  highlights the fields involved and the Code tab marks the line. Full
  OpenSCAD output is in the Console card.
- **Nothing appears on the plate (especially on Windows):** `4294967294` is
  the unsigned display of exit code -2 (`CLI_INVALID_PARAMS`), caused by the
  old `--single-instance <file>` launch argument being rejected before GUI
  startup. Version 0.9.3 removes that flag and uses the in-process
  `WM_COPYDATA` path first. Check the toast for IPC/fallback errors, then check
  Prepare; if missing, drag the STL from the exports list onto the plate.
- **Tracebacks:** `data_dir()/log/python_*.log`.

Code-mode OpenSCAD runs as a separate process with your user rights. Only run
code you trust.

## Development

```sh
cd frontend && npm ci && cd ..
python3 dev/serve.py                 # the real backend + page at http://127.0.0.1:8765/?theme=dark|light
xvfb-run -a python3 dev/thumbs.py    # re-render the model preview thumbnails
python3 packaging/bundle.py          # build the page and embed frontend, openscad/, and ai/
python3 packaging/bundle.py --check  # CI: fail if frontend/dist or orcad.py is stale
python3 -m pytest                    # backend, bootstrap, plugin and bundle tests
(cd frontend && npm test)            # bridge, catalog, mesh and storage tests
```

`dev/serve.py` simulates the host: it provides `window.orca`, OrcaSlicer's
theme variables and its injected element styles. You can work on the UI in a
normal browser against real OpenSCAD renders. When the real OpenSCAD test finds
a 2023+ `openscad` on `PATH`, it renders every catalog object.

Layout:

- `orcad.py`: plugin entry point, message protocol (`Session`), render queue,
  exports and plate handoff.
- `ai/`: pinned Node/Pi bootstrap, JSONL RPC bridge, OpenSCAD render extension,
  and the one-file AI workspace.
- `openscad/`: the standard-library backend. It holds `catalog.json` (the
  single source of truth for objects, parameters, tags, presets and the
  `libraries` registry, shared with the frontend), `validation.py`, `runner.py`
  (sandboxed argv, multi-root `OPENSCADPATH`, Manifold, timeouts, cancellation,
  STL cache), `mesh.py` (indexed preview meshes, 3MF writer), `bootstrap.py`
  (verified per-user install), `objects/*.scad` with the original shapes and
  `objects/wrappers/*.scad` with the wrappers that expose a vendored library as
  a catalog object, and the unmodified `vendor/` trees. See
  `openscad/README.md`.
- `frontend/`: React + Three.js page, built by Vite into one self-contained
  `dist/index.html`. It uses plain CSS on purpose, because OrcaSlicer injects
  unlayered element styles that would override `@layer`-based frameworks.
- `packaging/bundle.py`: deterministic release embedding.
- `assets/`: the app icon (`orcad-logo.png`) and a transparent-background
  mark (`orcad-mark.png`) for light documents.
- `.github/workflows/ci.yml`: tests plus the bundle freshness check; pushing a
  `v*` tag publishes the release with `orcad.py` attached. See `RELEASE.md`.

To add a catalog object, drop a `.scad` file in `openscad/objects/`, or wrap a
vendored library in `openscad/objects/wrappers/`, then describe its parameters,
tags and presets in `catalog.json` (add cross-field rules to `validation.py`
if needed) and run the bundler. Every variable in `catalog.json` has to appear
in the `.scad` file and every `include`/`use` in it has to resolve on
`OPENSCADPATH`; `tests/test_backend.py` enforces both.

## License

Original project code is AGPL-3.0-only (see `LICENSE`, `NOTICE`). The seven
vendored OpenSCAD libraries under `openscad/vendor/` keep their own licenses
(MIT, Apache-2.0 and CC0) and are unmodified; each one is attributed in
`NOTICE` and `THIRD_PARTY_NOTICES`. React, Three.js and the build
tools are listed in `THIRD_PARTY_NOTICES`. Downloaded OpenSCAD, Node.js, and Pi binaries/packages
remain under their respective upstream licenses.
