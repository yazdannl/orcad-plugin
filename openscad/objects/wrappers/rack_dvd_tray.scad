// orcad wrapper for jazwa/rackstack (MIT): rack-mount/tray/tray.scad.
// Upstream's catalog files hard-code every tray argument, so orcad exposes them here.
use <rackstack-8e296e93/rack-mount/tray/tray.scad>

tray_u = 1;
tray_width = 150;
tray_depth = 145;
tray_thickness = 3;
front_lip = 0;
back_lip = 0;
tray_padding = 15;
drive_slot = 143;
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
    // Slot the slim drive drops into.
    translate([(tray_width - drive_slot) / 2, (tray_depth - drive_slot) / 2, -1])
        cube([drive_slot, drive_slot, tray_thickness + 2]);
}
