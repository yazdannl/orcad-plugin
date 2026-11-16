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
  meshes, and writes 3MF. `mesh_native.py` loads the optional C fast path for
  the deduplication (`native/dist/<platform>/`, see `native/README.md`); when it
  is unavailable the Python loop runs instead, with identical output.
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
`license`, `repository` and an optional `author`. `catalog.py` turns it into `LIBRARIES`,
`LIBRARY_DIRS` (absolute paths) and `LIBRARY_SEARCH_PATH`. The **first** root is
Gridfinity Rebuilt, so `include <src/core/standard.scad>` written for it keeps
resolving; `vendor/` is on the path too, so a wrapper can address one library
unambiguously as `<rackstack-8e296e93/rack-mount/tray/tray.scad>`.

Those trees arrived batch by batch as the catalog grew, one pinned upstream
project at a time, and the table below lists every one of them. Most keep the
vendored files flat; some deliberately keep a layout that is not flat, because
upstream's own includes expect it: `bosl-*` keeps a `BOSL/` subdirectory
(`use <BOSL/involute_gears.scad>`), `catchnhole-*` keeps a `catchnhole/`
subdirectory (`use <catchnhole/catchnhole.scad>`), `lumpyscad-*` keeps
`lumpyscad/lib.scad` (`include <lumpyscad/lib.scad>`), and the mobility,
printer-accessory, science and mechanical trees keep upstream's own per-part or
per-sample directories (`Housings/`, `openscad/`, `src/`, `AuroraSCAD/`,
`lithophane lamp/`), which is how the authors' own projects consume them.

One upstream project appears twice: `gridfinity-rebuilt-openscad` (2.0.0) and
`gridfinity-rebuilt-openscad-v1`. 2.0.0 stopped drawing the side and back walls
of its spiral-vase bins (upstream issue #340), so the vase-mode bin is rendered
from the last 1.x revision, which is also the revision the Gridfinity Rebuilt
web generator uses for that model. Both trees sit next to each other on the
search path; OpenSCAD resolves the v1 file's own `<src/...>` includes next to
that file, so nothing mixes.

Nothing under `vendor/` is ever edited. Every tree keeps a `REVISION` file
whose first line is the pinned commit and which also records the upstream URL,
the license, the copyright holder and the vendored file list, next to the
upstream `LICENSE`. `tests/test_backend.py` fails if a revision drifts from the
catalog, if a license file goes missing, or if a library is no longer reachable
from the catalog.

| Catalog key | Vendored files | License | Copyright | Pinned revision |
| --- | --- | --- | --- | --- |
| `gridfinity-rebuilt-openscad` | `src/core`, `src/helpers`, `src/external`, the three top-level library files, upstream `README.md` | MIT | Kenneth Hodson; Zachary Freedman and Voidstar Lab LLC | `910e22d8607fd7f5f51ad5e5cbc5287a76810bfd` |
| `gridfinity-rebuilt-openscad-v1` | the four top-level library files, `src/core`, `src/helpers`, `src/external`, upstream `README.md` | MIT | Kenneth Hodson; Zachary Freedman and Voidstar Lab LLC | `c7dce9f6d595ba23b8bf798dd5069c600709697a` |
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
| `syncom-openscad-designs` | `bucket_and_lid.scad`, `chopstick_dishwasher_support.scad` | BSD-2-Clause | syncom | `a59bdd24c4ce8c1f0c5b68b7e8989e834bbab0f2` |
| `grimsqueaker-3d-printing` | `openscad/Kosmetik_Boxen.scad`, `openscad/Kosmetik_Boxen_flach.scad`, `openscad/einlegeboden_haken.scad` | MIT | GrimSqueaker | `0c0236f87645e2a61c8a2b47da3d7a3c7963241e` |
| `rorleth-3dknickknacks` | `OneWayInserts/one way insert.scad`, `TwineHolder/TwineHolder.scad`, `TwineHolder/threads.scad` | MIT | rorleth | `ddb4514417935098d9ef49307c143d724d5620fe` |
| `seifenschale` | `seifenschale.scad` | MIT | Christoph Steinbeck | `32ecd46741f6dbef2668c223c57e35126b745769` |
| `spice-rack-nomike` | `heart-icon.svg`, `infinity.svg`, `parametric-polyamory-spice-rack.scad` | BSD-2-Clause | nomike | `6c4453ee3bce657b1f61c316182e50c65977350d` |
| `openscad-scraper` | `scraper.scad` | CC0-1.0 | mnp (CC0 1.0 Universal public domain dedication) | `c16e115c34a9f836fd7b2fc484fd064c75f123f8` |
| `drlex-sieve` | `sieve.scad` | CC-BY-4.0 | DrLex (DrLex0) | `deead1a72d78eb6fbb8f1f579690743ac1dc2b27` |
| `drlex-screw-cap` | `CustomizableScrewCap.scad` | CC-BY-4.0 | DrLex (DrLex0) | `b20689e8980df83e64723eb0649fd49827c9a5b7` |
| `drlex-vase-mode-box` | `Vase_mode_box.scad` | CC-BY-4.0 | DrLex (DrLex0) | `5dbc4c92446ab0088dcc5ed080a2b079de8fb012` |
| `drlex-latch` | `CustomizableLatch.scad` | CC-BY-4.0 | DrLex (DrLex0) | `a7dcfe739f461a883e6dc0fc891ec3b19c92b712` |
| `drlex-pen-butler` | `PenButller.scad` | CC-BY-4.0 | DrLex (DrLex0) | `6450759098871fc7e94b32ce9d2d300c58ce794e` |
| `generic-coaster` | `GenericCoaster.scad` | MIT | Robert Joseph Korn (rjkorn, Open Valley Consulting Corp) | `95baec3c1e499c27b99ed1782cb50c9906cad78c` |
| `composable-organizer` | `common.scad`, `compartment.scad`, `lid.scad`, `parameters.scad`, `sizing.scad`, `tray-lid.scad` and 2 more | MIT | Maciej Małecki (mmalecki) | `f01bb2fb5547c58b28208eee58dee0c5305aa73d` |
| `belfryscad-bosl2` | `README.md`, `affine.scad`, `attachments.scad`, `ball_bearings.scad`, `beziers.scad`, `bosl1compat.scad` and 51 more | BSD-2-Clause | Revar Desmera (revarbat) and the BOSL2 contributors | `0c6f402bcb9ca968b7e623024335b55325635083` |
| `jointscad` | `README.md`, `dist/BridleJoint.scad`, `dist/DovetailJoint.scad`, `dist/DowelJoint.scad`, `dist/FingerJoint.scad`, `dist/JointSCAD.scad` and 3 more | MIT | Jonathan Law (HopefulLlama) | `447888065b30721267db6011faeaf8e4e95ee5ff` |
| `openscad-connectors` | `README.md`, `_defaults.scad`, `connector.scad`, `corner_bracket.scad`, `endcap.scad`, `grid.scad` and 2 more | MIT | Alex Gaudio (adgaudio) | `643a09d799a0d3bd2e2380c02aa46a0dc9c6f62a` |
| `opengrid-scad` | `README.md`, `opengrid-peg.scad`, `opengrid-snap.scad` | CC-BY-4.0 | Mikey Ward (mikeydotio) | `894a731f0dc6f487639dca8a91b6a3c8dfb76e8e` |
| `pantorouter-tenon-template` | `README.md`, `Tenons.scad` | CC0-1.0 | Chris Osborn (FozzTexx) | `eccd45775104de24f1480a478f650d34205d00a7` |
| `martmaiste-openscad-models` | `Cable-Comb/Cable-Comb.scad`, `DIN-Mount/DIN-Mount.scad`, `DIN-Mount/WeMos-Mini.scad`, `DIN-Mount/Serial-Energy-Meter-PCB-Cover.scad`, `D-Fruit-USB-HUB-Holder/D-Fruit-USB-HUB-Holder.scad`, `USB-Cap/USB-Cap.scad` and 2 more | MIT | martmaiste | `aded4de5c1acc0ebccb86c1b22a341fbd7724e3a` |
| `nas-rack` | `src/scad/inc/rack_item.scad`, `src/scad/rpiDrive.scad` | Apache-2.0 | Ilias Stergiou (istergiou) | `8c5f15827c6c871172a4fa9bb259b6e4f21044e4` |
| `fabz` | `fan-grille/fan_grille.scad` | CC0-1.0 | Jonathan Freedman (otakup0pe) | `4efbc18e0bd137458d6a9b36da6c5f44e644c0de` |
| `nilskrause-cad` | `esp_case.scad`, `azdelivery_esp8266_adapter_board.scad`, `esp_8266.scad`, `connector_pins.scad`, `microsdcard-holder.scad` | MIT | Nils Krause (NilsKrause) | `9c4d69e98e561e542ef9ca178dcafcef96db9b38` |
| `pkoehlers-rpi-case` | `rpi_case.scad` | MIT | pkoehlers | `a19a32e5932638bef0882c65189a3dbc5447e313` |
| `pcb-enclosure` | `pcb-enclosure.scad`, `pcb-enclosure-test.scad` | BSD-3-Clause | Rafael G. Martins | `ce261a389da419c767efd607159fe80f20f16d17` |
| `marble-run-jhpieper` | `openscad/curve.scad`, `openscad/funnel.scad`, `openscad/straight-extrude.scad` | MIT | Jan Pieper (jhpieper) | `64b8f62091775c56ffcdbe276b6a8cbad95581f7` |
| `openscad-dice-models-nemon` | `dice2n.scad`, `dice4.scad`, `dice12.scad`, `dice20.scad` | CC0-1.0 | nemon- and contributors (public domain dedication) | `be03f70f7edbb7c6bcef9ddbcbd91f3776314178` |
| `dice-tower-hirthwork` | `bricks.scad`, `common.scad`, `rocks.scad`, `steps.scad`, `tower.scad` | BSD-2-Clause | hirthwork and the dice-tower.scad contributors | `c130e05a22bc602768cc8ff1bd7e2e4a515f9d32` |
| `fidget-spinner-sharpcoder` | `src/fidget-spinner.scad`, `src/parameters.scad` | MIT | Joshua Cole (SharpCoder) | `5e67c0e739bd8ffbd109217d7b5a329b4d2b9692` |
| `brick-lib-qubolino` | `brick.scad`, `corner.scad`, `globals.scad`, `panel.scad`, `windscreen.scad` | CC0-1.0 | qubolino and contributors (public domain dedication) | `95a3c86563aa40bf221b019271b683dfa048077f` |
| `technic-scad-cfinke` | `Technic.scad` | MIT | Christopher Finke (cfinke) | `41f17a4696b582850097a2e3779348bc27c87f47` |
| `puzzlecad` | `LICENSE-MIT`, `puzzlecad.scad`, `puzzlecad/puzzlecad-2d.scad`, `puzzlecad/puzzlecad-boxes.scad`, `puzzlecad/puzzlecad-burr.scad`, `puzzlecad/puzzlecad-burr-stick.scad` and 6 more | MIT | Aaron Siegel (aaron-siegel) | `2ba064c51e8b8c0018a34d96776b33625c938602` |
| `3d-inserts-christianlutz` | `uprising/cards.scad`, `uprising/resources-tray.scad` | CC0-1.0 | Christian Lutz and contributors (public domain dedication) | `5a9976a9469ad90ece2e94347ef616120302cd17` |
| `holgerlembke-openscad-modelle` | the eight printer-accessory models (spool holders, Ender 3 guides, bed-level test, scraper holder, cable strain relief, V-slot clamp) | MIT | Holger Lembke | `b48f4ea0a3c11ae1b0e538a567b954c682798d5b` |
| `splice3d` | `common.scad` and the spool holder, filament guide, guide jig, tool accessory and cable management sources | MIT | Splice3D Contributors | `1b9233f1b07b923ec7b08eaba5db74e2b7fa6790` |
| `gchristopher-3dprinting` | the five Rostock MAX V2 effector-platform sources (hot-end plate, clamp, fan shroud, wider platform) | Apache-2.0 | Gregg Christopher | `8fdb722ced4222dcd4c2eab56f08b1f3aade0aaf` |
| `ewc3-test-hole-generator` | the current V2 and archived V1 test-hole plate generators | MIT | EWC3 Labs | `c4b7bfdcafb22f276b4a8f03ce6b62be92850e9b` |
| `bicycle-lights` | the five light housings under `Housings/` (two front lights, rear light, 18650 battery box, switch holder) | MIT | Guillaume Chauvat | `4d7effa69b957d27221d2b94aad71b3463367253` |
| `cycling-wall-rack` | the four `scad/` parts (wall bracket, glasses holder, tray, S-hook) | MIT | Antoine CARON | `3ca19cc7ca0962809868cbd203be34b1a6f0e98a` |
| `ridercz-gopro` | `GoPro.scad` and the extension-arm and phone-holder samples | MIT | Michal Altair Valasek | `98c0161e58d4d912481cdf280b573563e221b2a7` |
| `connector-adapter` | `connector-adapter.scad` | MIT | regenschein71 | `80e4636ed2176e46106b748a2c26215a2e372f75` |
| `bottlecage` | `bottlecage.scad`, `bottlecage-compact.scad` | CC0-1.0 | Markus Gutschke (public domain dedication) | `15484334e49ac12be6ac3d21a1f48207b739b3df` |
| `triathlon-bike` | the gel clip, brake spacer and saddle bottle holder sources | MIT | joseph touzet | `1e55632a064bcac4068acec52910953fb0f49271` |
| `clip-openscad` | `components/assembly.scad` with the constants, phone, mirror and rounded-cube files it uses | MIT | Standard Cyborg | `789c87dc951308470e7d9229527f154c237fb95e` |
| `corrugated-pipe-clamp` | `brim.scad`, `corner_bend.scad`, `corner_elbow.scad`, `corner_elbow_clear.scad`, `corrugation.scad`, `panel_spigot.scad` and 3 more | MIT | agjendem | `f85ac05dca676f67dd6591add7f6e5ab35981062` |
| `matrixflow` | `matrixflow.scad` | MIT | Rudi Szabó | `2938da2202df1a993cda6c920cc9d1d891703236` |
| `pythocrates-openscad-models` | `curtain_rod_caps/curtain_rod_caps.scad`, `garage_ventilation_grille/grille.scad`, `lock_plate/lock_plate.iscad`, `loudspeaker_wall_mount/wall_mount.scad`, `shower_curtain_adapter/rail_mount.scad`, `tolerances/hinges.scad` | CC0-1.0 | Pythocrates | `85e20833292a19d51bc617a6ea98f308ad541cb9` |
| `aurora-concrete` | `honeycomb_mold/honeycomb_mold.scad`, `mold_ASTM_C39_cylinder.scad` | Unlicense | AuroraRoboticsLab | `fc6bf17038d55c76fa27962c94933f6064fc3324` |
| `parametric-hinge` | `parametric_hinges.scad` | MIT | Tom (HarkonenBade) | `6195704fe34b5f249a3a067f1808ea0c467b46e3` |
| `parametric-openscad-generators` | `Projects/Mounting_plate_generator_parametric.scad`, `toolbox.scad` | MIT | Lourenço Guerreiro | `e91cfac6a96dfe2513489d62b670d768c19f7378` |
| `whosawhatsis-geometry` | `README.md`, `Polygons+Polyhedrons/platonicSolids.scad`, `Polygons+Polyhedrons/edge_platonic_solids.scad`, `Nets/platonic_net.scad`, `Reuleaux/meissner.scad`, `Reuleaux/reuleaux-n-gon.scad` and 1 more | CC-BY-4.0 | Joan Horvath and Rich Cameron | `0aaf3e49385d07b08e03564476bae8124e284138` |
| `cg-rassin-rocketry` | `3D_models/KeplerMK1_NoseCone.scad`, `3D_models/KeplerMK1_GimbalTestStand.scad` | MIT | Charles Grassin | `7b7783a369f8496e1e0b4fc6bd2372f3161ea0b3` |
| `erellaz-spider` | `vane_support_120_degrees_double_v0.scad`, `vane_support_90_degrees_double_v0.scad` | MIT | erellaz | `ee9711effbcb949e20f4f52fdde4bbbde37c7574` |
| `erellaz-telescope-ring` | `Esprit100_ring_v5.scad` | MIT | erellaz | `aef1b9b51967fc1b24e1891ebd3dfad582100f62` |
| `nikon-diaphot-tmd` | `3d_printing_files/nikon_diaphot_45mm_filter_holder.scad`, `3d_printing_files/nikon_diaphot_universal_holder_taper.scad`, `3d_printing_files/nikon_diaphot_universal_holder_clamp.scad` | MIT | Will Ivy | `d86aeaaa0edbd100c16ab3f61799bd4820c0d16f` |
| `equatorial-sundial` | `equatorial_sundial.scad`, `analemma_groove.scad`, `analemma_2030.scad` | MIT | RyanDMott | `5ece8a9cdabb0969b9534ca3c5d1009226347e4e` |
| `astro-replicas` | `Accessories/dovetail_bar.scad`, `Accessories/dovetail_saddle.scad` | MIT | Hongming | `363b6388f06b337deb8de44bc2d657833eb4cfd6` |
| `giguere-periodic-table` | `giguere3d.scad` | MIT | William H. Grover | `c1b07199ec36a11a75f1a7bd54531bfbe12365ae` |
| `azdle-printables` | `unsorted/tealight-rack.scad`, `unsorted/marker-rack.scad`, `honeycomb-honeycomb/honeycomb-honeycomb.scad` | CC0-1.0 | Patrick Barrett | `158c8e86016c82332b5d6116dfd015b9a48a9479` |
| `doofdoofsf-3dmodels` | `lib/rounding.scad`, `table_coasters/coaster.scad`, `fancy_joint/coaster.scad`, `light_shade/light_shade.scad`, `flower_charger/definitions.scad`, `flower_charger/flower_pot.scad` | MIT | John Quinn | `a578682933dc96ad7aa6fd8c17d36a4025e6ffc4` |
| `elliotf-reprap-household-misc` | `lib/util.scad`, `lampshade/util.scad`, `lampshade/open_top_fluted.scad`, `lampshade/enclosed_and_more_dim.scad`, `shelves/book_end.scad`, `garden/bottom_watering_pot.scad` | MIT | Elliot Foster | `c353c8acee63b75c19fc63be89bb2649d56aa580` |
| `filippyrek-scad-3d-print-models` | `lithophane lamp/lithophane lamp.scad` | MIT | Filip Pýrek | `cd4852ada0d325ef4035a643f75cb316f5efab61` |
| `lumpyscad` | `lumpyscad/lib.scad` | MIT | Elliot Foster | `49526b23fca8ba0142f5a139c6be3033836b76a9` |
| `mjseabright-lampshades` | `geometry_functions.scad`, `mounting_functions.scad`, `rope_swirl/rope_swirl.scad`, `rope_swirl/rope_swirl_large.scad`, `turbine/turbine.scad`, `turbine/turbine_large.scad` and 4 more | MIT | Matthew Seabright | `5c20d7018cca118b88825590cf27469256e557ff` |
| `xoob-parametric-picture-frame` | `parametric-picture-frame.scad`, `lib/profiles.scad`, `lib/segmentation.scad`, `lib/ornament.scad`, `lib/clips.scad` | MIT | Alexander Brausewetter | `84e8314b4b4b4bd4673b1428c5e8d4ab8b3b4f49` |
| `aurorascad` | `AuroraSCAD/gear.scad`, `AuroraSCAD/gearbox.scad`, `AuroraSCAD/motor.scad`, `AuroraSCAD/perego_gearbox.scad`, `AuroraSCAD/screw.scad` | Unlicense | Dr. Orion Lawlor and the Aurora Robotics Lab | `172751a1c9c6b08e1c89355fcb2926f87f8c0f22` |
| `ball-joint` | `balljoint.scad`, `helix_extrude.scad` | CC0-1.0 | Gael Lafond | `85720f535a680c4ad389d8090977aec0d98560a9` |
| `ball-bearing` | `ball-bearing.scad` | MIT | Blomart Cédric | `684fb2fbc9b6d25f5a52746ae384acc393cba5ca` |
| `cycoidal-gear` | `cycloidal-disc.scad`, `cycloidal-gear.scad` | MIT | marble | `0295817cdce7bd7979f6dd49b61074673f2aaf50` |
| `omni-wheel-hub` | `screws.scad`, `servo-hub.scad` | MIT | Kevin J. Walchko | `3a5e25dedb8b6ac991ecb2873b28f15a81473df6` |
| `lib-robo-core` | `core_config_dimensions.scad`, `core_parts_wheels.scad`, `core_robo_functions.scad`, `torus.scad` | MIT | Don Korte | `2e0cc474f5cb7d79298e1dde4e9dfa7ef4528a74` |
| `donkbot` | `openscad_donkbot/dkb_core.scad`, `openscad_donkbot/dkb_lib_parts_boards.scad`, `openscad_donkbot/dkb_lib_parts_camera.scad`, `openscad_donkbot/dkb_lib_parts_motors.scad`, `openscad_donkbot/dkb_lib_parts_switches_batts.scad`, `openscad_donkbot/dkb_lib_plate_components.scad` and 4 more | MIT | Don Korte | `0cae5346ba4b529f92fe0e24ce445bc7e1d474bb` |
| `servo-brake-actuator` | `design/servo_brake_drum.scad` | MIT | Lassaad Mahmoudi | `a61c9b8b7ff592d6fb3c37d25efe519f10cd26c8` |
| `saxonrah-robot-parts` | `Robot_U_Bracket_ServoType.scad` | MIT | Robert Valentine | `d9ff23cf4921a707fee2312d5c9c257a9bcd65c9` |

Upstream URLs are in the catalog and in each `REVISION` file; the full
attribution is in `NOTICE` and `THIRD_PARTY_NOTICES` in the repository root.

## Attribution on model cards

Any object whose source lives under `vendor/` is credited in the UI: the model
card shows a small **info** button whose popover names the library, its
license, the author and the repository (in addition to `NOTICE` and
`THIRD_PARTY_NOTICES`). An object may also carry its own
`attribution: {label, license, author?, repository?}` entry, which overrides the
library's values - use it when a wrapper is based on a specific upstream file
or when the library entry alone is not specific enough (for example a
CC-BY-4.0 model that requires naming its author in the product). Objects under
`objects/` written for this plugin carry neither and show no button.

**No font files are vendored.** The split-flap tree ships Roboto and Epilogue
TTF files that `flap.scad` and `label.scad` `use` for their optional letter
previews, so any object that reaches those files renders with
`Can't read font` errors even with the letters disabled. The catalog therefore
uses only the font-free parts of that tree: the flap card, spool and the jigs
and tools under `3d/tools/`. Upstream's front panel (`3d/combined_front_panel.scad`)
is vendored but not exposed as an object, because its own include chain reaches
the font files.
