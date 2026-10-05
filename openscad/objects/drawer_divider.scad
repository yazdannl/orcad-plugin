// orcad: drawer divider panel with a glue flange, a stacking slot and a finger grip.
length = 200;
height = 45;
thickness = 3;
flange_width = 12;
flange_thickness = 3;
slot_width = 3.4;
slot_depth = 20;
top_radius = 3;
grip_width = 14;
grip_depth = 8;

flange_w = max(flange_width, thickness + 2);
flange_t = min(flange_thickness, height / 2);
r = min(top_radius, thickness / 2 * 0.9);
grip_w = min(grip_width, length - 4);
grip_d = min(grip_depth, height - flange_t - 2, max(grip_w / 2 - 1, 1));

// Side profile in the y/z plane, extruded along x.
profile = [
    [-flange_w / 2, 0],
    [flange_w / 2, 0],
    [flange_w / 2, flange_t],
    [thickness / 2, flange_t],
    [thickness / 2, height - r],
    [thickness / 2 - r, height],
    [-thickness / 2 + r, height],
    [-thickness / 2, height - r],
    [-thickness / 2, flange_t],
    [-flange_w / 2, flange_t]
];

difference() {
    rotate([90, 0, 90])
        linear_extrude(height = length, center = true)
            polygon(profile);

    // Slot that lets a second divider cross this one at right angles.
    if (slot_width > 0)
        translate([-length / 2 - 1, -slot_width / 2, 0])
            cube([length + 2, slot_width, min(slot_depth, height)]);

    // Finger grip in the top edge.
    if (grip_w > 0 && grip_d > 0)
        hull()
            for (x = [-grip_w / 2 + grip_d, grip_w / 2 - grip_d])
                translate([x, -thickness, height])
                    rotate([-90, 0, 0])
                        cylinder(d = 2 * grip_d, h = 3 * thickness);
}
