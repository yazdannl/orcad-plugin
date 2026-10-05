// orcad wrapper for rcolyer/threads-scad (CC0): MetricNut and MetricWasher.
use <threads-scad-4ae9aeb3/threads.scad>

nut_diameter = 4;
nut_thickness = 0;
nut_quantity = 3;
nut_pitch = 12;
nut_tolerance = 0.4;

for (i = [0 : nut_quantity - 1]) {
    translate([i * nut_pitch, 0, 0]) MetricNut(nut_diameter, nut_thickness, tolerance = nut_tolerance);
    translate([i * nut_pitch, nut_diameter + 2, 0]) MetricWasher(nut_diameter);
}
