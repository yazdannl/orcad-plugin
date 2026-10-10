// orcad wrapper for mmalecki/catchnhole (MIT): bolt_head().
use <catchnhole-99428972/catchnhole/catchnhole.scad>

nut_names = ["M3", "M4", "M5", "M6", "M8"];
nut_size_index = 1;
plate_width = 40;
plate_depth = 40;
plate_thickness = 8;
head_clearance = 0.1;
head_top_clearance = 0.5;
bolt_kind = "countersunk";

difference() {
    cube([plate_width, plate_depth, plate_thickness]);
    bolt(nut_names[nut_size_index], length = plate_thickness, kind = bolt_kind,
         head_diameter_clearance = head_clearance,
         head_top_clearance = head_top_clearance);
}
