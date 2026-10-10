// orcad wrapper for mmalecki/catchnhole (MIT): bolt() and bolt_head().
use <catchnhole-99428972/catchnhole/catchnhole.scad>

nut_names = ["M3", "M4", "M5", "M6", "M8"];
nut_kind = "hexagon";
bolt_kind = "socket_head";
nut_size_index = 1;
plate_width = 40;
plate_depth = 40;
plate_thickness = 8;
head_top_clearance = 0.5;

difference() {
    cube([plate_width, plate_depth, plate_thickness]);

    // Through hole for the shank, sized by the library from the bolt name.
    bolt(nut_names[nut_size_index], length = plate_thickness, kind = "headless");

    // Pocket for the head, cut down from the top face.
    translate([0, 0, plate_thickness])
        bolt_head(nut_names[nut_size_index], kind = bolt_kind,
                  head_top_clearance = head_top_clearance);
}
