// orcad wrapper for mmalecki/catchnhole (MIT): bolt_head() and bolt().
use <catchnhole-99428972/catchnhole/catchnhole.scad>

nut_names = ["M3", "M4", "M5", "M6", "M8"];
nut_kind = "hexagon";
bolt_kind = "hex_head";
nut_size_index = 1;
block_width = 30;
block_depth = 20;
block_height = 16;
nut_height_clearance = 0.5;
nut_width_clearance = 0.2;

difference() {
    cube([block_width, block_depth, block_height]);

    translate([0, 0, block_height])
        bolt_head(nut_names[nut_size_index], kind = bolt_kind,
                  head_diameter_clearance = nut_width_clearance,
                  head_top_clearance = nut_height_clearance);

    bolt(nut_names[nut_size_index], length = block_height, kind = "headless",
         head_diameter_clearance = nut_width_clearance);
}
