// orcad wrapper for kennetek/gridfinity-rebuilt-openscad (MIT): gridfinity-rebuilt-bins.scad.
//
// The vendored file declares every knob below itself, so these declarations are the catalog's
// names for them: they keep the object's source readable on its own and mirror upstream's defaults
// (the catalog passes its own values as -D, and a -D value is applied before evaluation, so it
// wins). The bin also exposes constants upstream keeps in src/core/standard.scad - wall thickness,
// magnet hole size and depth, label tab size - declared here for the same reason; the values
// standard.scad derives from them (MAGNET_HOLE_DEPTH, REFINED_HOLE_HEIGHT, TAB_POLYGON and the tab
// support height) follow an override because they are computed while that file is read.

/* [General] */
gridx = 3;
gridy = 2;
half_grid = false;

/* [Height] */
gridz = 6;
gridz_define = 0;
height_internal = 0;
enable_zsnap = false;
include_lip = true;

/* [Walls] */
d_wall = 0.95;

/* [Compartments] */
divx = 1;
divy = 1;
depth = 0;
cut_cylinders = false;
cd = 10;
c_chamfer = 0.5;

/* [Compartment features] */
style_tab = 1;
place_tab = 0;
scoop = 1;
TAB_WIDTH_NOMINAL = 42;
_tab_depth = 15.85;

/* [Base holes] */
only_corners = false;
refined_holes = true;
magnet_holes = false;
screw_holes = false;
crush_ribs = true;
chamfer_holes = true;
printable_hole_top = true;
enable_thumbscrew = false;
MAGNET_HOLE_RADIUS = 3.25;
REFINED_HOLE_RADIUS = 2.93;
MAGNET_HEIGHT = 2;

include <gridfinity-rebuilt-openscad-910e22d8/gridfinity-rebuilt-bins.scad>
