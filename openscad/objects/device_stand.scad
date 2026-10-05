// orcad: wedge stand that holds a phone or small tablet upright in a slot.
stand_width = 90;
stand_depth = 70;
stand_height = 65;
lean_angle = 20;
wall_thickness = 4;
base_thickness = 4;
lip_height = 12;
device_thickness = 11;
slot_clearance = 1.5;

slot_gap = device_thickness + slot_clearance;
slot_back_y = wall_thickness + slot_gap;
panel_root = max(0, base_thickness - 2);
corner_r = min(wall_thickness, 6);
back_length = (stand_height - panel_root) / cos(lean_angle) - corner_r;
base_y = max(stand_depth, slot_back_y + wall_thickness + 10);

// The leaning back panel: rounded top corners, rounded upper body, flat end sunk into the base.
module back_panel() {
    translate([0, slot_back_y, panel_root])
        rotate([-lean_angle, 0, 0])
            hull()
                for (x = [corner_r, stand_width - corner_r], z = [corner_r, back_length])
                    translate([x, wall_thickness, z])
                        rotate([90, 0, 0])
                            cylinder(d = 2 * corner_r, h = wall_thickness);
}

translate([-stand_width / 2, 0, 0])
    union() {
        cube([stand_width, base_y, base_thickness]);
        cube([stand_width, wall_thickness, base_thickness + lip_height]);
        back_panel();
    }
