// orcad wrapper for revarbat/BOSL (BSD-2-Clause): rack.
use <bosl-4ce427a8/BOSL/involute_gears.scad>

rack_pitch = 5;
rack_teeth = 20;
rack_thickness = 5;
rack_height = 10;
rack_pressure_angle = 20;

rack(mm_per_tooth = rack_pitch, number_of_teeth = rack_teeth, thickness = rack_thickness, height = rack_height, pressure_angle = rack_pressure_angle);
