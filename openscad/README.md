# orcad OpenSCAD backend

Standard-library-only Python package used by `orcad.py`. The release bundler
also embeds it into the single-file plugin.

- `catalog.json` is the single source of truth for every object: label,
  category, icon, description, `.scad` source, and each parameter's exact
  OpenSCAD variable, type, default, range, step, unit, options, group, help,
  `depends_on` and `conflicts_with`. A parameter's type is `number`, `integer`,
  `boolean` or `text`; a text parameter also carries `max_length`, `max_lines`,
  `rows` and `placeholder` for the frontend's textarea. Every object also
  carries a `tags` list
  (search terms the frontend matches on top of key, label and description) and
  a `presets` list of ready-made parameter sets: `[{name, description?,
  params: {variable: value}}]`, where every `params` key is one of that
  object's own variables and the result validates against the defaults. The
  frontend imports the same file at build time. It also holds the quality
  profiles (`$fa`/`$fs`: draft 12/0.8, balanced 6/0.3, final 3/0.1) and the
  `libraries` map described below.
- `validation.py` validates parameters strictly (types, ranges, steps, options,
  text length/line/control-character limits, cross-field rules) and reports
  errors as `{"fields": [...]}`.
- `runner.py` probes OpenSCAD (2023 or newer; 2021.01 is rejected) and
  renders with safe argv lists (`-D name=value`, binary STL, the Manifold
  kernel, `--enable=textmetrics` for the nameplate's text layout). String
  values are escaped into a quoted `-D` value. Each render has a timeout, can be cancelled cooperatively, and runs
  with a scrubbed environment. stderr is captured into readable errors with
  line numbers. Results go into a size-bounded, content-addressed STL cache
  keyed by object/code, parameters, quality, engine version and library
  revision. `OPENSCADPATH` holds every library root, joined with `os.pathsep`
  (`;` on Windows) in catalog order, plus `vendor/` itself; `catalog.py`
  exposes that list as `LIBRARY_SEARCH_PATH` and the combined digest of all
  pinned revisions as `LIBRARY_REVISION`.
- `mesh.py` validates STL output, deduplicates vertices for compact preview
  meshes, and writes 3MF.
- `bootstrap.py` installs a verified official OpenSCAD snapshot per user when
  none is on `PATH`. See the main README for the platform table and the
  newest-snapshot fallback.
- `objects/*.scad` holds the original shapes, written for this plugin and
  editable in place.

## Objects: originals and wrappers

Most catalog objects are either an original file under `objects/` or a
**wrapper** under `objects/wrappers/`. A wrapper is a small original `.scad`
file that points at an unmodified vendored library and exposes it as a catalog
object:

- It declares every catalog parameter with upstream's own default, so `-D`
  overrides work whether or not the vendored file is `include`d or `use`d
  (`use` imports no variables, which is why those values are restated).
- It instantiates the upstream module with those parameters, usually laying
  several copies out on one bed.
- Where a pinned revision is broken, it may define the missing functions or
  modules around the upstream call instead of patching the vendored file. The
  comment at the top of the wrapper records why.
- `objects/wrappers/splitflap_flap.scad` also repeats a small upstream module
  (`flap_2d()`), because the only file that exports it pulls in font binaries
  that are deliberately not vendored.

Eleven objects need no wrapper at all: upstream already renders them the way
the catalog wants, so their `source` points straight into the vendored tree and
the catalog owns only the parameter ranges.

Three wrapper shapes cover the libraries that are not plain `use`-and-call:

- **String options.** catchnhole picks a bolt from a name string that `-D`
  cannot set, so the wrapper maps an option index onto `["M3", "M4", "M5",
  "M6", "M8"]` and passes `nut_names[nut_size_index]`.
- **Cut-only modules.** BOSL's `nema_mount_holes()` and catchnhole's
  `nutcatch_*` only subtract material, so the wrapper cuts them out of a block
  or plate of its own.
- **Include-time dimensions.** YAPP_Box sizes a box from top-level assignments
  and derives many of them while the file is read, so that wrapper declares the
  values with upstream's own variable names - a `-D` value then arrives before
  the file derives anything - and calls `YAPPgenerate()` itself with `debug`
  turned on, because upstream only builds a box under that flag.

## Vendored libraries

`catalog.json`'s `libraries` map is the registry: one entry per vendored tree
with `label`, `root` (relative to `openscad/`), `revision` (the pinned commit),
`license` and `repository`. `catalog.py` turns it into `LIBRARIES`,
`LIBRARY_DIRS` (absolute paths) and `LIBRARY_SEARCH_PATH`. The **first** root is
Gridfinity Rebuilt, so `include <src/core/standard.scad>` written for it keeps
resolving; `vendor/` is on the path too, so a wrapper can address one library
unambiguously as `<rackstack-8e296e93/rack-mount/tray/tray.scad>`.

Eleven of those trees were added for the Building, Technic, Mechanical, Rounded
shapes, Smooth shapes, Cases, Fasteners, Controls, Cycling, Computer hardware
and Rocketry categories. Two of them deliberately keep a layout that is not
flat, because upstream's own includes expect it: `bosl-*` keeps a `BOSL/`
subdirectory (`use <BOSL/involute_gears.scad>`), and `catchnhole-*` keeps a
`catchnhole/` subdirectory (`use <catchnhole/catchnhole.scad>`), which is how
the author's own projects consume it.

Nothing under `vendor/` is ever edited. Every tree keeps a `REVISION` file
whose first line is the pinned commit and which also records the upstream URL,
the license, the copyright holder and the vendored file list, next to the
upstream `LICENSE`. `tests/test_backend.py` fails if a revision drifts from the
catalog, if a license file goes missing, or if a library is no longer reachable
from the catalog.

| Catalog key | Vendored files | License | Copyright | Pinned revision |
| --- | --- | --- | --- | --- |
| `gridfinity-rebuilt-openscad` | `src/core`, `src/helpers`, `src/external`, the two top-level library files, upstream `README.md` | MIT | Kenneth Hodson; Zachary Freedman and Voidstar Lab LLC | `910e22d8607fd7f5f51ad5e5cbc5287a76810bfd` |
| `gridfinity-openscad` | 9 Gridfinity modules (baseplate, basic cup, cup modules, chess, FLSUN Q5 cup, glue stick, modules, silverware, socket holder) | MIT | Jamie (vector76) | `0e7308cd8fc7fb4191aa69d81175a90d10de751c` |
| `openscad-gridfinity-block` | `gridfinity_block.scad` | Apache-2.0 | wromijn and the Gridfinity Block contributors | `6ef6d644fff81283c810c2b90197901ce6657f84` |
| `gridfinity-basket-openscad` | `gridfinityBasket.scad` | MIT | LeKoYa and the Gridfinity Block contributors | `549dc4015e4511daeb7b942de96d2531101701db` |
| `threads-scad` | `threads.scad` | CC0-1.0 | rcolyer and contributors (public domain dedication) | `4ae9aeb3b136f9858200f77a304b909a000ce3b4` |
| `splitflap` | the 25 `3d/**.scad` files (flap, spool, front panel, PCB, tools) | Apache-2.0 | Scott Bezek and the splitflap contributors | `87b17c531ca57b0bf10e86754e9d6b404b11a131` |
| `rackstack` | the `rack-mount/`, `helper/`, `config/` and `rack/` OpenSCAD sources | MIT | Zhao Wang (jazwa) | `8e296e935aad89a6d1a5023da79becc634c10c2d` |
| `lego-scad` | `LEGO.scad` | MIT | Christopher Finke | `d717ca8e29dbb62f271bf351b35179060eaa744e` |
| `open-bricks-technic` | the 45 sources under `parts/` and `globals/` | MIT | Joerg Dettweiler (jaydee69) | `0465e456fbfa8a12772cc3ba7e4aff3f4cbe2487` |
| `bosl` | the `BOSL/*.scad` modules the mechanical objects use (gears, joiners, bearings, threads, shapes) | BSD-2-Clause | Revar Desmera (revarbat) | `4ce427a8a38786e5f74b728c1e33d9fe7d4904d2` |
| `round-anything` | `polyround.scad`, `MinkowskiRound.scad`, `unionRoundMask.scad` | MIT | Kurt Hutten (Irev-Dev) | `061fef7c429628808e847696bb345a9b0ec6e279` |
| `smooth-prim` | `smooth_prim.scad` | CC0-1.0 | Ryan A. Colyer (public domain dedication) | `0d0038f984465f3eb0f6026a4d5de2f79d2dcea8` |
| `yapp-box` | `YAPPgenerator_v3.scad` | MIT | Willem Aandewiel (mrWheel) | `f9400c419ef1dea7dc0b3607876989b4f3faa2b7` |
| `catchnhole` | `catchnhole/catchnhole.scad`, `catchnhole/bolts.json`, `catchnhole/nuts.json` | MIT | Maciej Malecki (mmalecki) | `99428972ca2588f5ce33c0df54d097a14acf7f10` |
| `openscad-knobs` | `knob.scad` | MIT | Maciej Malecki (mmalecki) | `ae3344fa8b312a8625ddbf9328a28e71f95183d7` |
| `bike-mounts` | `bottle-cage.scad`, `handlebar.scad` | MIT | Maciej Malecki (mmalecki) | `55d636c45ec94d63732bd32591f42083e3099f09` |
| `param-case` | the 12 case modules (defaults, fan, feet, front_panel, gpu, heatsink, mini-itx, motherboard, pci_bracket, power_switch, psu, vent) | BSD-2-Clause | Nirav Patel (eclecticc) | `b5f1ee43c1db93cac7657c52bcc5a97baa5df7c4` |
| `rocket-fins` | `fins.scad` and the three ready-made fin sets | BSD-2-Clause | Adrian Schlatter | `57f2c9fd475be499aafb2655b12f20a75f135e60` |

Upstream URLs are in the catalog and in each `REVISION` file; the full
attribution is in `NOTICE` and `THIRD_PARTY_NOTICES` in the repository root.

**No font files are vendored.** The split-flap tree ships Roboto and Epilogue
TTF files that `flap.scad` and `label.scad` `use` for their optional letter
previews, so any object that reaches those files renders with
`Can't read font` errors even with the letters disabled. The catalog therefore
uses only the font-free parts of that tree: the flap card, spool and the jigs
and tools under `3d/tools/`. Upstream's front panel (`3d/combined_front_panel.scad`)
is vendored but not exposed as an object, because its own include chain reaches
the font files.
