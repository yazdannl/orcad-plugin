// orcad wrapper for eclecticc/ParametricCase (BSD-2-Clause): vent.scad.
use <param-case-b5f1ee43/vent.scad>

plate_width = 100;
plate_height = 60;
plate_thickness = 3;
vent_pitch = 5;
vent_wall = 1.5;

difference() {
    cube([plate_width, plate_height, plate_thickness]);
    translate([plate_width / 2, plate_height / 2, 0])
        vent_circular(plate_height / 2, vent_pitch, vent_wall);
}
