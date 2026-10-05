# Changelog

## 0.9.11 — Expanded model catalog (2026-10-05)

- **49 models in six categories.** Gridfinity grew from two objects to 19
  (bins, baseplates, blocks, pockets, baskets, a full chess set, glue stick,
  silverware, socket holder, FLSUN Q5 cup) and three more categories joined it:
  Organization, Cases and Panels. Fasteners and Basics came along with the
  pinned libraries they need.
- **The model list scrolls.** 49 objects no longer fit the tab, so the catalog
  list is its own capped, keyboard-focusable scroll region (`role="group"`,
  labelled "Models") with a styled thin scrollbar instead of growing the page.
- **Seven pinned upstream libraries.** Gridfinity Rebuilt, gridfinity_openscad,
  openscad-gridfinity-block, gridfinity-basket-openscad, threads-scad, splitflap
  and rackstack are vendored unmodified under `openscad/vendor/`, each with its
  upstream `LICENSE` and a `REVISION` file naming the commit, the license, the
  copyright holder and the vendored file list. `NOTICE` and
  `THIRD_PARTY_NOTICES` carry the attribution; `tests/test_backend.py` fails if
  a revision drifts from `catalog.json` or a license file goes missing.
- **Multi-library code mode.** `OPENSCADPATH` now holds every library root,
  joined with the platform path separator and ordered so `include <src/...>`
  still resolves Gridfinity Rebuilt first. The render cache key includes every
  pinned revision, so upgrading one library cannot serve another's cached STL.
- **Presets and tags.** Every object carries a `tags` list for search and a
  `presets` list of named parameter sets; a preset applies on top of the
  current values, shows as active until you edit a value, and can never set an
  illegal combination of options. All 153 presets are validated against their
  object's own parameters by the test suite.
- **Original models.** battery holder, PCB mount, cable clip, device stand,
  wall hook, bit holder, drawer divider and wall plate are new `.scad` written
  for this plugin, alongside the six original shapes.
- **Split-flap flap cards are blank by design.** Upstream draws the flap
  letters with `text()` and reaches its Roboto/Epilogue fonts through files that
  are not vendored, so every render of those models ended in
  `Can't read font` errors. The flap card is now built from upstream's
  font-free dimension files plus its own `flap_2d()` outline - byte-identical
  card geometry, no font dependency, score it and write the characters by hand.
  The connector case and the front panel are dropped from the catalog for the
  same reason: upstream's `combined_front_panel.scad` reaches the font files
  through its own include chain, so it can only be exposed once that is split
  upstream. Their font-free parts - the spool, the jigs and the flap itself -
  all stay.

## 0.9.10 — Renamed to OrcaCAD (2026-10-04)

- The plugin is now **OrcaCAD**. "OrCAD" is a registered trademark of Cadence
  Design Systems, so the display name no longer uses it: the tab, the plugin name
  in the Plugins dialog, the page header and window title, the AI panel labels,
  every message the plugin shows, the README and the release guide.
- Installs keep updating in place. The plugin key is OrcaSlicer's, derived from
  the file stem for a local install and from the cloud UUID for a cloud install,
  so the file names (`orcad.py`, `orcad_any.py`) and the repository name are
  unchanged - renaming those would break the cloud connection and the upload
  suffix rule.
- The on-disk locations also keep their names on purpose: `%LOCALAPPDATA%\orcad`
  and its siblings hold the downloaded OpenSCAD build, the private Pi agent
  credentials and the render cache, and the `orcad.settings` localStorage key
  holds the UI settings. Renaming any of them would make every existing install
  start over.

## 0.9.9 — Publishing matches the setup guide

- The publish workflow is now named `Publish to OrcaCloud` with the guide's job
  permissions, since the OIDC token carries the workflow name.
- Publishing can be re-run by hand: with no tag and no release event the script
  falls back to the newest published release, so a rejected publish is one
  **Run workflow** click away instead of a new tag.

## 0.9.8 — OrCAD tab icon and name, refined holes opt-in

- The tab now reads **OrCAD** instead of `orcad`, matching the plugin's display
  name in the Plugins dialog and on Orca Cloud. The plugin key (`orcad`) and the
  file name are unchanged, so installs keep resolving the same way.
- The tab carries the plugin logo. `get_icon()` returns `orcad-icon.png` from
  beside `orcad.py` when it exists and otherwise unpacks the embedded copy into
  the per-user cache (`%LOCALAPPDATA%\orcad`, `~/Library/Caches/orcad`,
  `~/.cache/orcad`) and returns that path. A failure there is silent: the tab
  simply draws no icon, which is what an empty path means upstream.
- Gridfinity **refined holes are now off by default**. They change the base
  profile of every bin, so a fresh bin is the classic one unless refined holes
  are switched on; magnet holes still conflict with them.

## 0.9.7 — Publish from the workflow Orca Cloud knows

- The GitHub release now carries `orcad_any.py` next to `orcad.py` (identical
  bytes): `orcad_any.py` is the name carrying the universal target suffix that
  Orca Cloud requires, so an upload or a release picked up from GitHub finds a
  valid file whichever name it looks for.
- Publishing to Orca Cloud moved out of `ci.yml` into
  `.github/workflows/publish-orcacloud.yml`, the release workflow named in the
  portal connection, and now also runs on the tag push itself. The API rejects a
  token from a workflow that is not the connected release workflow with
  "A valid GitHub Actions OIDC token from a connected release workflow is
  required.", which is what publishing from `ci.yml` was hitting.
- The publish step reports which workflow it ran as when the API rejects the
  identity, so the connection can be matched against the run that failed.

## 0.9.6 — Diagnosable release runs

- The Orca Cloud publish step now reports its failure as a GitHub Actions
  annotation with the API's own message, so a rejected publish is visible in the
  run summary instead of only in the job log.
- Creating the GitHub release is idempotent: re-running the release job refreshes
  the notes of an existing release instead of failing, so a failed publish can be
  retried.

## 0.9.5 — Backend unpacked to a short cache path

- The embedded OpenSCAD backend is now unpacked into the per-user cache
  (`%LOCALAPPDATA%\orcad\backend\<hash>`, `~/Library/Caches/orcad/backend`,
  `~/.cache/orcad/backend`) instead of `.backend/` beside `orcad.py`. A plugin
  installed from Orca Cloud lives in
  `orca_plugins/_subscribed/<user>/<uuid>/`, which is already ~160 characters;
  together with the vendored Gridfinity paths the deepest extracted file reached
  270 characters, past Windows' 260-character limit, and the single-file plugin
  failed to load from there with an import error that carried no detail.
- If the unpacked tree cannot be renamed into place (a Windows lock, or another
  process winning the race) the complete staging tree is used instead of being
  deleted, so a load never ends up pointing at a directory that does not exist.
- An unpack failure now raises a message naming the target directory instead of
  surfacing an empty import error in the Plugins dialog.

## 0.9.4 — Release automation

- Added GitHub Actions CI on Linux, Windows and macOS: frontend tests, the
  pytest suite and `packaging/bundle.py --check`, which fails when the committed
  `frontend/dist/index.html` or the blobs embedded in `orcad.py` are stale.
- Pushing a `v*` tag now publishes a GitHub release with the verified
  `orcad.py` attached and the matching CHANGELOG section as release notes.
- Made the test suite run on Windows: fake OpenSCAD/Pi binaries are created as
  `.cmd` wrappers there, POSIX file-mode assertions and shebang-only bootstrap
  tests are skipped, and the POSIX send-to-plate test pins the platform.
- Fixed the embedded backend archive recording the build host: `ZipInfo`
  defaults `create_system` to Windows, so a Windows or macOS build produced
  different blob bytes than the committed one. The archive is now byte-identical
  everywhere, with a regression test.
- Documented the release procedure in `RELEASE.md` and documented installing a
  released file with **Plugins ▾ Install local plugin**.

## 0.9.3 — Corrected Windows plate handoff

- On Windows, send the saved STL through OrcaSlicer's in-process
  `WM_COPYDATA` handler from the render worker, using the upstream
  `wxWindowNR`/instance-property discovery and C-style escaped UTF-16 payload.
  Orca queues the received path for its normal `Plater::load_files()` importer;
  the synchronous message return does not confirm the import completed.
- Corrected the executable fallback to pass the STL as a positional file
  argument without `--single-instance`. Upstream's CLI parser rejects that flag
  before GUI startup (`CLI_INVALID_PARAMS`, -2); GUI single-instance behavior is
  selected from Orca's app configuration. macOS keeps `open -a`.
- Verified the same Windows IPC pattern in the public Gridfinity Orca plugin and
  OrcaSlicer Model Search plugin. Added mocked Windows protocol and fallback
  tests; real Orca/Windows runtime testing is still required.

## 0.9.2 — Windows plate handoff

- Attempted the `--single-instance <file>` executable route. Source review for
  0.9.3 found that Orca's CLI parser rejects this flag before GUI startup and
  returns -2, so this release's Windows fallback could not work as intended.
- On Windows, create the child without a console, wait for the process to exit,
  and report timeout/nonzero-exit failures instead of claiming the file was
  sent. Success messaging said to verify the plate.
- Documented Windows handoff troubleshooting and tested `.stl` file handling
  in OrcaSlicer's normal file-open forwarding source.

## 0.9.1 — In-app provider setup

- Added orcad-managed Pi OAuth/subscription and API-key sign-in/sign-out using Pi's SDK auth API and private `auth.json`.
- Added custom OpenAI-compatible endpoints for Ollama, LM Studio, vLLM, and similar services, with optional `/models` discovery and 0600 key files.
- Added provider status/model settings and transient authentication dialogs; saved credentials are not returned to page state or logs.
- Improved provider settings, Revert/New chat buttons, and the prompt Send button layout.

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
- **Send to plate** opens the STL with OrcaSlicer (`open -a` on macOS); the
  Windows `--single-instance` attempt was corrected in 0.9.3 after source review
  showed the CLI rejects that flag.
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
