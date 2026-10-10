// orcad wrapper for revarbat/BOSL (BSD-2-Clause): gear.
use <bosl-4ce427a8/BOSL/involute_gears.scad>

gear_pitch = 5;
gear_teeth = 20;
gear_thickness = 8;
gear_hole = 5;
gear_pressure_angle = 20;

gear(mm_per_tooth = gear_pitch, number_of_teeth = gear_teeth, thickness = gear_thickness, hole_diameter = gear_hole, pressure_angle = gear_pressure_angle);
