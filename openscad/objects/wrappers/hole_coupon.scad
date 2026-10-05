// orcad wrapper for rcolyer/threads-scad (CC0): a printable coupon that checks a hole.
// The upstream holes cut their children, so the coupon is the child. Only one branch runs
// per render: OpenSCAD 2023 mis-computes children() when one imported module is
// instantiated more than once in the same expression.
use <threads-scad-4ae9aeb3/threads.scad>

hole_style = 0; // [0:Clearance, 1:Countersunk, 2:Tapped]
hole_diameter = 3;
hole_depth = 8;
sink_diameter = 6;
coupon_length = 40;
coupon_width = 30;
coupon_thickness = 8;
hole_offset = 0;
hole_tolerance = 0.3;

if (hole_style == 0) {
    ClearanceHole(hole_diameter, coupon_thickness * 3,
                  position = [hole_offset, 0, 0], tolerance = hole_tolerance) {
        cube([coupon_length, coupon_width, coupon_thickness]);
    }
} else if (hole_style == 1) {
    CountersunkClearanceHole(hole_diameter, coupon_thickness * 3,
                             position = [hole_offset, 0, coupon_thickness], rotation = [180, 0, 0],
                             sinkdiam = sink_diameter, tolerance = hole_tolerance) {
        cube([coupon_length, coupon_width, coupon_thickness]);
    }
} else {
    ScrewHole(hole_diameter, hole_depth,
              position = [hole_offset, 0, coupon_thickness], rotation = [180, 0, 0],
              tolerance = hole_tolerance) {
        cube([coupon_length, coupon_width, coupon_thickness]);
    }
}
