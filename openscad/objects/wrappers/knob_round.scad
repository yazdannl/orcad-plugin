// orcad wrapper for mmalecki/openscad-knobs (MIT): knob().
use <openscad-knobs-ae3344fa/knob.scad>

knob_d = 32;
knob_h = 10;
knob_chamfer = 0.5;
knob_stem_d = 0;
knob_stem_h = 0;
knob_points = 4;
knob_center = true;

knob_shape = "round";

knob(knob_d, knob_h, knob_shape, chamfer = knob_chamfer,
     stem_d = knob_stem_d, stem_h = knob_stem_h,
     star_points = knob_points, center = knob_center);
