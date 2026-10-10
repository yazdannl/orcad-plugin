// orcad wrapper for mmalecki/openscad-bicycle-mounts (MIT): bottle_cage_mount().
use <bike-mounts-55d636c4/bottle-cage.scad>

plate_width = 100;
plate_height = 20;
plate_thickness = 5;
bolt_length = 3;
bolt_spacing = 64;
bolt_tolerance = 10;
head_clearance = 2;
head_top_clearance = 5;
bolt_size = "M5";
bolt_kind = "socket_head";

difference() {
    cube([plate_width, plate_height, plate_thickness]);
    translate([10, plate_height / 2, 0])
        bottle_cage_mount(bolt_length, bolt_size, bolt_kind, bolt_spacing,
                          bolt_tolerance, head_clearance, head_top_clearance);
}
