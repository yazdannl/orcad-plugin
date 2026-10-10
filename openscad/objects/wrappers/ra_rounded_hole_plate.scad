// orcad wrapper for Irev-Dev/Round-Anything (MIT): polyround.scad.
use <round-anything-061fef7c/polyround.scad>
use <smooth-prim-0d0038f9/smooth_prim.scad>
plate_width = 60;
plate_height = 40;
plate_thickness = 6;
hole_diameter = 20;
hole_radius = 2;

difference() {
    polyRoundExtrude([[hole_radius, hole_radius, hole_radius],
                      [plate_width - hole_radius, hole_radius, hole_radius],
                      [plate_width - hole_radius, plate_height - hole_radius, hole_radius],
                      [hole_radius, plate_height - hole_radius, hole_radius]],
                     length = plate_thickness, r1 = hole_radius, r2 = hole_radius);
    translate([plate_width / 2, plate_height / 2])
        SmoothHole(radius = hole_diameter / 2, height = plate_thickness + 1,
                   smooth_rad = hole_radius);
};
