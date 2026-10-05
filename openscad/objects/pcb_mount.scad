// orcad: tray that carries a printed circuit board on four screw-in stand-offs.
board_length = 85;
board_width = 56;
hole_spacing_x = 58;
hole_spacing_y = 49;
mount_hole_diameter = 3.2;
standoff_diameter = 6;
standoff_height = 8;
base_thickness = 3;
wall_height = 4;

base_margin = standoff_diameter + 2;
base_x = board_length + 2 * base_margin;
base_y = board_width + 2 * base_margin;

difference() {
    union() {
        cube([base_x, base_y, base_thickness]);

        // Rim, when the board should sit in a shallow tray instead of on bare stand-offs.
        if (wall_height > 0)
            translate([0, 0, base_thickness])
                difference() {
                    cube([base_x, base_y, wall_height]);
                    translate([base_margin, base_margin, -0.01])
                        cube([board_length + 0.2, board_width + 0.2, wall_height + 0.02]);
                }

        for (x = [-1, 1], y = [-1, 1])
            translate([base_x / 2 + x * hole_spacing_x / 2, base_y / 2 + y * hole_spacing_y / 2, 0])
                cylinder(d = standoff_diameter, h = base_thickness + standoff_height);
    }

    for (x = [-1, 1], y = [-1, 1])
        translate([base_x / 2 + x * hole_spacing_x / 2, base_y / 2 + y * hole_spacing_y / 2, -0.01])
            cylinder(d = mount_hole_diameter, h = base_thickness + standoff_height + 0.02);
}