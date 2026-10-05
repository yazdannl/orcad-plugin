// orcad wrapper for rcolyer/threads-scad (CC0): MetricWoodScrew.
use <threads-scad-4ae9aeb3/threads.scad>

screw_diameter = 4;
screw_length = 30;
screw_quantity = 3;
screw_pitch = 12;
screw_tolerance = 0.4;

for (i = [0 : screw_quantity - 1]) {
    translate([i * screw_pitch, 0, 0]) MetricWoodScrew(screw_diameter, screw_length, tolerance = screw_tolerance);
}
