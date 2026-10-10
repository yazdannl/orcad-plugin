// orcad wrapper for jazwa/rackstack (MIT): rack-mount/tray/tray.scad.
// Upstream's catalog files hard-code every tray argument, so orcad exposes them here.
use <rackstack-8e296e93/rack-mount/tray/tray.scad>

tray_u = 3;
tray_width = 175;
tray_depth = 110;
tray_thickness = 3;
front_lip = 7.5;
back_lip = 7.5;
tray_padding = 2.5;
catch_side = 12;
catch_inset = 20;
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
    // Nut catch slots for the switch's wall fixings.
    for (x = [-tray_width / 2 + catch_inset, tray_width / 2 - catch_inset])
        translate([x, tray_depth / 2, -1]) rotate([0, 0, 45])
            cube([catch_side, catch_side, tray_thickness + 2]);
}
