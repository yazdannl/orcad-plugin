// orcad wrapper for kennetek/gridfinity-rebuilt-openscad (MIT): gridfinity-spiral-vase.scad
// from the 1.x revision pinned in vendor/gridfinity-rebuilt-openscad-c7dce9f (see that tree's
// REVISION: 2.0.0 no longer draws the vase bin's side and back walls, upstream issue #340).
//
// The vendored file declares the vase bin's own knobs itself, so these declarations are the
// catalog's names for them and mirror the web generator's defaults; the catalog passes its own
// values as -D, and a -D value is applied before evaluation, so it wins. MAGNET_HOLE_RADIUS and
// MAGNET_HEIGHT are constants of src/core/standard.scad, declared here for the same reason;
// the values standard.scad derives from MAGNET_HEIGHT while it is read follow an override.

/* [Component] */
type = 0;

/* [Printer] */
nozzle = 0.6;
layer = 0.35;
bottom_layer = 3;

/* [General] */
gridx = 1;
gridy = 1;
gridz = 6;
l_grid = 42;
n_divx = 1;

/* [Features] */
enable_lip = true;
enable_scoop_chamfer = true;
enable_funnel = true;
enable_pinch = true;
enable_inset = false;

/* [Base holes] */
enable_holes = true;
MAGNET_HOLE_RADIUS = 3.25;
MAGNET_HEIGHT = 2.1;

/* [Styles] */
style_tab = 0;
a_tab = 40;
style_base = 0;

include <gridfinity-rebuilt-openscad-c7dce9f/gridfinity-spiral-vase.scad>
