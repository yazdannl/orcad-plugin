// orcad wrapper for vector76/gridfinity_openscad (MIT): gridfinity_basic_cup.scad.
// Upstream selects the label tab and lip style with string variables, which -D cannot set, and
// it sizes the cup from its own top-level assignments, so orcad declares the values the catalog
// exposes here and rewrites them after the include.
label_option = 0; // [0:disabled, 1:left, 2:right, 3:center]
lip_option = 0; // [0:normal, 1:reduced, 2:none]
cup_width = 2;
cup_depth = 1;
cup_height = 3;
cup_chambers = 1;
cup_magnet_diameter = 6.5;
cup_screw_depth = 0;
cup_fingerslide = true;
cup_label_width = 0;
cup_wall_thickness = 0.95;
cup_floor_thickness = 0.7;
label_names = ["disabled", "left", "right", "center"];
lip_names = ["normal", "reduced", "none"];

include <gridfinity_openscad-0e7308cd/gridfinity_basic_cup.scad>

withLabel = label_names[label_option];
lip_style = lip_names[lip_option];
width = cup_width;
depth = cup_depth;
height = cup_height;
chambers = cup_chambers;
magnet_diameter = cup_magnet_diameter;
screw_depth = cup_screw_depth;
fingerslide = cup_fingerslide;
labelWidth = cup_label_width;
wall_thickness = cup_wall_thickness;
floor_thickness = cup_floor_thickness;