// orcad wrapper for mmalecki/catchnhole (MIT).
use <catchnhole-99428972/catchnhole/catchnhole.scad>

nut_names = ["M3", "M4", "M5", "M6", "M8"];
nut_kind = "hexagon";
bolt_kind = "headless";
nut_size_index = 1;
nut_height_clearance = 0.1;
nut_width_clearance = 0.2;
bolt_length = 16;
bolt_head_clearance = 0.1;
block_width = 30;
block_depth = 20;
block_height = 16;

difference() {
    cube([block_width, block_depth, block_height]);

    nutcatch_sidecut(nut_names[nut_size_index], kind = nut_kind,
        height_clearance = nut_height_clearance,
        width_clearance = nut_width_clearance);

    bolt(nut_names[nut_size_index], length = bolt_length, kind = bolt_kind,
         head_diameter_clearance = bolt_head_clearance);
}
