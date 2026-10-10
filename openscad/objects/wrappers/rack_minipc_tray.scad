// orcad wrapper for jazwa/rackstack (MIT): rack-mount/tray/tray.scad.
// Upstream's catalog files hard-code every tray argument, so orcad exposes them here.
use <rackstack-8e296e93/rack-mount/tray/tray.scad>

tray_u = 4;
tray_width = 130;
tray_depth = 110;
tray_thickness = 3;
front_lip = 3;
back_lip = 5;
tray_padding = 15;
slot_size = 18;
slot_slope = 12;
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
    // Corner clearance slots, as in the upstream catalog model.
    translate([-4, 5, 18]) {
        rotate([0, 90, 0]) rotate([0, 0, 45]) cube([slot_size, slot_size, 155]);
        translate([0, 28, -4]) rotate([0, 90, 0]) rotate([0, 0, 45]) cube([slot_slope, slot_slope, 150]);
        translate([0, 48, -6.5]) rotate([0, 90, 0]) rotate([0, 0, 45])
            cube([slot_slope - 4, slot_slope - 4, 145]);
    }
}
