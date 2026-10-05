// orcad: screw-down clip run that holds a row of round cables against a wall or panel.
cable_diameter = 6;
clip_count = 2;
clip_pitch = 14;
base_thickness = 3;
base_margin = 8;
web_thickness = 1.5;
clip_height = 0;
open_top = true;
mount_hole_diameter = 4;

channel_diameter = cable_diameter + 0.4;
// An open clip cradles half the cable, a closed one snaps a full ring around it.
clip_body_height = clip_height > 0 ? clip_height :
    (open_top ? channel_diameter / 2 : channel_diameter) + web_thickness;
clip_body_width = channel_diameter + 2 * web_thickness;
clip_width = max(clip_pitch - web_thickness, channel_diameter + 1);
base_x = clip_count * clip_pitch + 2 * base_margin;
base_y = max(clip_body_height, clip_body_width) + 2 * base_margin;
channel_z = base_thickness + channel_diameter / 2;

difference() {
    union() {
        cube([base_x, base_y, base_thickness]);
        for (i = [0 : clip_count - 1])
            translate([base_margin + i * clip_pitch, base_y / 2, base_thickness])
                cube([clip_width, clip_body_width, clip_body_height]);
    }

    translate([base_x / 2 - 5, base_y / 2, channel_z + (open_top ? channel_diameter / 2 : 0)])
        rotate([0, 90, 0])
            cylinder(h = base_x + 10, d = channel_diameter, center = true);

    // Screw holes sit in front of and behind the cables so the heads stay clear.
    for (i = [0 : clip_count - 1], y = [-1, 1])
        translate([base_margin + i * clip_pitch + clip_width / 2,
                   base_y / 2 + y * (max(clip_body_height, clip_body_width) / 2 + base_margin / 2), -0.01])
            cylinder(d = mount_hole_diameter, h = base_thickness + 0.02);
}
