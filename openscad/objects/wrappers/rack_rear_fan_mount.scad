// orcad wrapper for jazwa/rackstack (MIT): rack-mount/tray/tray.scad.
// Upstream's catalog files hard-code every tray argument, so orcad exposes them here.
use <rackstack-8e296e93/rack-mount/tray/tray.scad>

tray_u = 12;
tray_width = 130;
tray_depth = 35;
tray_thickness = 3;
front_lip = 126;
back_lip = 126;
tray_padding = 25;
fan_opening = 120;
fan_spacing = 105;
fan_hole = 5;
mount_offset_x = 20;
mount_offset_y = 20;
mount_spacing_x = 100;
mount_spacing_y = 100;

difference() {
    bottomScrewTray(
        u = tray_u,
        trayWidth = tray_width,
        trayDepth = tray_depth,
        trayThickness = tray_thickness,
        frontLipHeight = front_lip,
        backLipHeight = back_lip,
        mountPoints = [[mount_offset_x, mount_offset_y],
                     [mount_offset_x + mount_spacing_x, mount_offset_y],
                     [mount_offset_x, mount_offset_y + mount_spacing_y],
                     [mount_offset_x + mount_spacing_x, mount_offset_y + mount_spacing_y]],
        frontThickness = tray_thickness,
        sideThickness = tray_thickness,
        mountPointElevation = 1,
        mountPointType = "m3",
        sideSupport = true,
        trayLeftPadding = tray_padding
    );
    // Case fan opening and its screw holes.
    translate([tray_width / 2, tray_depth / 2, -1])
        rotate([90, 0, 0]) cylinder(h = tray_depth + 2, d = fan_opening);
    for (s = [[-1, -1], [-1, 1], [1, -1], [1, 1]])
        translate([tray_width / 2 + s[0] * fan_spacing / 2,
                   tray_depth / 2 + s[1] * fan_spacing / 2, -1])
            rotate([90, 0, 0]) cylinder(h = tray_depth + 2, d = fan_hole);
}
