<img src="assets/orcad-logo.png" width="96" align="right" alt="orcad logo">

# orcad: parametric CAD tab for OrcaSlicer (v0.9.9)

orcad adds an **orcad** tab next to Prepare / Preview / Device in OrcaSlicer. It
lets you pick a parametric model, tune it with live 3D preview, and put it on
the build plate with one click.

- **Library:** Gridfinity Bin and Gridfinity Baseplate, rendered from the pinned
  upstream [Gridfinity Rebuilt](https://github.com/kennetek/gridfinity-rebuilt-openscad)
  source, plus a Box, Cylinder, Tube and Mounting plate. Every parameter gets a
  slider, number field, switch or option picker, with inline validation.
- **Code:** an OpenSCAD editor with examples, line numbers, error-line
  highlighting and Ctrl+Enter to render. `include <src/...>` loads the bundled
  Gridfinity library, so you can build your own bins.
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

## How it works

Every model is rendered by **OpenSCAD** with its fast Manifold kernel, so a
typical Gridfinity bin takes well under a second. The plugin has **no Python
dependencies** (`dependencies = []`). OpenSCAD is provisioned on first use if
needed; the AI runtime is optional and downloads only when you choose **Install
pi** in the Code view.

On first start the plugin looks for OpenSCAD 2023 or newer on `PATH`. If it
finds none, it downloads an official OpenSCAD development snapshot (about 80 MB)
into a per-user cache, verifies it against a pinned SHA-256 digest, and never
asks for admin rights:

| Platform | Artifact | Cache |
| --- | --- | --- |
| Linux x86_64 / ARM64 | AppImage, extracted, so FUSE is not required | `~/.cache/orcad/openscad` |
| macOS (Intel + Apple Silicon) | universal `.dmg` | `~/Library/Caches/orcad/openscad` |
| Windows x86_64 | portable `.zip` | `%LOCALAPPDATA%\orcad\openscad` |

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

The runtime, `model.scad` workspace, orcad-managed Pi agent directory, and
custom endpoint key files live under the per-user `orcad/ai` cache:

| Platform | AI cache |
| --- | --- |
| Linux | `$XDG_CACHE_HOME/orcad/ai` or `~/.cache/orcad/ai` |
| macOS | `~/Library/Caches/orcad/ai` |
| Windows | `%LOCALAPPDATA%\orcad\ai` |

In Code → Settings, choose **Use my pi setup** to reuse `~/.pi/agent`, or
**orcad-managed** for a separate account/configuration under the cache above.
Managed setup supports Pi's built-in browser/device sign-in and API-key login
(e.g. GitHub Copilot, Anthropic Claude Pro/Max, and OpenAI ChatGPT), plus Sign
out. Pi itself stores those credentials in
`orcad/ai/private-agent/auth.json`; orcad never returns the saved values to the
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
plus orcad's local OpenSCAD render tool; it has no shell tool. **Privacy:**
prompts and the current OpenSCAD source are sent to the selected model provider
for inference. OpenSCAD rendering itself runs locally. Do not send designs you
are not comfortable sharing with that provider. AI-generated code can be unsafe;
inspect it before using or printing it.

**Send to plate** writes an STL to `exports/` beside the plugin. On Windows,
orcad's render worker finds the current-process `wxWindowNR` main frame carrying
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
Nightly). Builds without it get an "orcad (needs a newer OrcaSlicer)" entry
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

Restart OrcaSlicer and enable **orcad** in the Plugins dialog. Runtime data
lives next to the plugin: `exports/` (your files) and `.cache/` (rendered STL
cache, capped at 512 MB). For a single-file install the OpenSCAD backend is
unpacked into the per-user cache instead of beside the file
(`%LOCALAPPDATA%\orcad\backend`, `~/Library/Caches/orcad/backend`,
`~/.cache/orcad/backend`), because a plugin directory installed from Orca Cloud
is already a long path. The AI cache location is listed above.

## Troubleshooting

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
  single source of truth for objects and parameters, shared with the
  frontend), `validation.py`, `runner.py` (sandboxed argv, Manifold, timeouts,
  cancellation, STL cache), `mesh.py` (indexed preview meshes, 3MF writer),
  `bootstrap.py` (verified per-user install), `objects/*.scad` and the
  unmodified `vendor/` Gridfinity tree.
- `frontend/`: React + Three.js page, built by Vite into one self-contained
  `dist/index.html`. It uses plain CSS on purpose, because OrcaSlicer injects
  unlayered element styles that would override `@layer`-based frameworks.
- `packaging/bundle.py`: deterministic release embedding.
- `assets/`: the app icon (`orcad-logo.png`) and a transparent-background
  mark (`orcad-mark.png`) for light documents.
- `.github/workflows/ci.yml`: tests plus the bundle freshness check; pushing a
  `v*` tag publishes the release with `orcad.py` attached. See `RELEASE.md`.

To add a catalog object, drop a `.scad` file in `openscad/objects/`, describe
its parameters in `catalog.json` (add cross-field rules to `validation.py` if
needed) and run the bundler.

## License

Original project code is AGPL-3.0-only (see `LICENSE`, `NOTICE`). The vendored
Gridfinity Rebuilt source is MIT and unmodified. React, Three.js and the build
tools are listed in `THIRD_PARTY_NOTICES`. Downloaded OpenSCAD, Node.js, and Pi binaries/packages
remain under their respective upstream licenses.
