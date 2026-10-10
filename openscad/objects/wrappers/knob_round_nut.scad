// orcad wrapper for mmalecki/openscad-knobs and catchnhole (both MIT).
use <openscad-knobs-ae3344fa/knob.scad>
use <catchnhole-99428972/catchnhole/catchnhole.scad>

knob_d = 32;
knob_h = 12;
knob_chamfer = 0.5;
knob_stem_d = 6;
knob_stem_h = 12;
nut_size_index = 1;
nut_height_clearance = 0.1;
nut_catch = true;
nut_names = ["M3", "M4", "M5", "M6", "M8"];
bolt_kind = "headless";

// The knob's children are subtracted from the head, so the nut catch is a child.
knob(knob_d, knob_h, "round", chamfer = knob_chamfer,
     stem_d = knob_stem_d, stem_h = knob_stem_h, center = true)
    if (nut_catch)
        translate([0, 0, knob_h]) nutcatch_parallel(nut_names[nut_size_index],
                                                  height_clearance = nut_height_clearance);
