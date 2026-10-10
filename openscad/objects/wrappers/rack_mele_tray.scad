// orcad wrapper for jazwa/rackstack (MIT): rack-mount/tray/tray.scad.
// Upstream's catalog files hard-code every tray argument, so orcad exposes them here.
use <rackstack-8e296e93/rack-mount/tray/tray.scad>

tray_u = 2;
tray_width = 145;
tray_depth = 88;
tray_thickness = 3;
front_lip = 3;
back_lip = 2;
tray_padding = 10;
mount_offset_x = 27.5;
mount_offset_y = 34;
mount_spacing_x = 80;
mount_spacing_y = 0;

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
}
