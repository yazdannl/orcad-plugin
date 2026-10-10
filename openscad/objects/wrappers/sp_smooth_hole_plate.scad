// orcad wrapper for rcolyer/smooth-prim (CC0-1.0): difference.
use <smooth-prim-0d0038f9/smooth_prim.scad>

plate_width = 60;
plate_height = 40;
plate_thickness = 8;
hole_radius = 6;
hole_smooth_rad = 2;

difference() {
    cube([plate_width, plate_height, plate_thickness]);
    translate([plate_width / 2, plate_height / 2]) SmoothHole(radius = hole_radius, height = plate_thickness + 1, smooth_rad = hole_smooth_rad);
};
