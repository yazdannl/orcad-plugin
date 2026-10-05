// orcad: wall hook with a J-shaped arm, screw holes or a glue pad; prints flat, back face down.
hook_reach = 30;
hook_diameter = 8;
back_height = 22;
upturn = 10;
base_thickness = 4;
mount_style = 0; // [0:Screw holes, 1:Glue pad, 2:Pad and holes]
mount_hole_diameter = 5;
mount_spacing = 24;
mount_offset = 12;
pad_diameter = 20;
gusset_length = 12;

r = hook_diameter / 2;
reach_center = max(hook_reach - r, r + 1);
// The arm sinks into the plate so the two always share a solid joint.
bar_z = base_thickness - min(base_thickness / 2, 2) + r;
hole_y = -(r + mount_offset);
hole_low = hole_y - (mount_spacing > 0 ? mount_spacing / 2 : 0);
edge_margin = max(3, mount_hole_diameter / 2 + 2);
plate_y_min = (mount_style == 1) ? -r - edge_margin : hole_low - mount_hole_diameter / 2 - edge_margin;
plate_y_max = max(back_height, hook_diameter) + edge_margin;
pad_thickness = 1.5;
pad_d = min(pad_diameter, 2 * min(plate_y_max, -plate_y_min) - 2);
rib = min(hook_diameter, 6);

// The arm: a bar along x with a rounded tip that turns up into a short leg.
module arm() {
    hull() {
        translate([0, -r, bar_z]) rotate([-90, 0, 0]) cylinder(d = hook_diameter, h = hook_diameter);
        translate([reach_center, -r, bar_z]) rotate([-90, 0, 0]) cylinder(d = hook_diameter, h = upturn + hook_diameter);
    }
}

module flat_hook() {
    difference() {
        union() {
            // Back plate, lying flat so the mounting face is on the bed.
            translate([-base_thickness, plate_y_min, 0])
                cube([base_thickness, plate_y_max - plate_y_min, base_thickness]);

            // Gusset carrying the arm off the plate.
            if (gusset_length > 0)
                rotate([90, 0, 0])
                    linear_extrude(height = rib, center = true)
                        polygon([[0, bar_z], [gusset_length, bar_z], [0, base_thickness]]);

            arm();
        }

        if (mount_style != 1)
            for (y = (mount_spacing > 0 ? [hole_y - mount_spacing / 2, hole_y + mount_spacing / 2] : [hole_y]))
                translate([-base_thickness - 0.01, y, base_thickness / 2])
                    rotate([0, 90, 0]) cylinder(d = mount_hole_diameter, h = base_thickness + 0.02);
    }

    if (mount_style > 0)
        translate([-base_thickness - pad_thickness, 0, base_thickness / 2])
            rotate([0, 90, 0]) cylinder(d = pad_d, h = pad_thickness + 0.01);
}

// Lay the printed plate flat: x (out from the wall) stays, y (up the wall) becomes z.
translate([0, base_thickness / 2, 0])
    rotate([90, 0, 0])
        flat_hook();
