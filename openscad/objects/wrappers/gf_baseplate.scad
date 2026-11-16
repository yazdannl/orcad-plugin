// orcad wrapper for kennetek/gridfinity-rebuilt-openscad (MIT):
// gridfinity-rebuilt-baseplate.scad.
//
// The vendored file declares every baseplate knob itself, so these declarations are the catalog's
// names for them and mirror upstream's defaults; the catalog passes its own values as -D, and a -D
// value is applied before evaluation, so it wins. l_grid is upstream's deprecated alias for the
// grid cell size and is what the baseplate's own call passes on, so it is the one to expose. The
// magnet pocket radius is a constant of src/core/standard.scad, declared here for the same reason;
// MAGNET_HOLE_DEPTH is derived from it while that file is read, so an override carries through.

/* [General] */
gridx = 1;
gridy = 1;
l_grid = 42;

/* [Screw-together] */
d_screw = 3.35;
d_screw_head = 5;
screw_spacing = 0.5;
n_screws = 1;

/* [Fit to drawer] */
distancex = 0;
distancey = 0;
fitx = 0;
fity = 0;

/* [Styles] */
style_plate = 3;
style_hole = 0;

/* [Mounting] */
enable_magnet = true;
crush_ribs = true;
chamfer_holes = true;
MAGNET_HOLE_RADIUS = 3.25;

include <gridfinity-rebuilt-openscad-910e22d8/gridfinity-rebuilt-baseplate.scad>
