// orcad wrapper for rcolyer/threads-scad (CC0): MetricCountersunkBolt.
use <threads-scad-4ae9aeb3/threads.scad>

bolt_diameter = 4;
bolt_length = 16;
bolt_quantity = 3;
bolt_pitch = 12;
bolt_tolerance = 0.4;

for (i = [0 : bolt_quantity - 1]) {
    translate([i * bolt_pitch, 0, 0]) MetricCountersunkBolt(bolt_diameter, bolt_length, tolerance = bolt_tolerance);
}
