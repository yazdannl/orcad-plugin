// orcad wrapper for jazwa/rackstack (MIT): rack-mount/tray/tray.scad.
// Upstream's catalog files hard-code every tray argument, so orcad exposes them here and
// builds the mount-point grid from the tray size. mountPoints is a vector -D cannot set.
tray_u = 1;
tray_width = 140;
tray_depth = 140;
tray_thickness = 3;
front_lip_height = 6;
back_lip_height = 6;
tray_padding = 15;
tray_hole_diameter = 0;
tray_hole_x = 70;
tray_hole_y = 70;

use <rackstack-8e296e93/rack-mount/tray/tray.scad>

difference() {
    bottomScrewTray(
        u = tray_u,
        trayWidth = tray_width,
        trayDepth = tray_depth,
        trayThickness = tray_thickness,
        frontLipHeight = front_lip_height,
        backLipHeight = back_lip_height,
        mountPoints = [
            [tray_padding, tray_padding],
            [tray_width - tray_padding, tray_padding],
            [tray_padding, tray_depth - tray_padding],
            [tray_width - tray_padding, tray_depth - tray_padding]
        ],
        frontThickness = 3,
        sideThickness = 3,
        mountPointElevation = 1,
        mountPointType = "m4",
        sideSupport = true,
        trayLeftPadding = tray_padding
    );

    // Optional cut-out through the tray floor (fan, cable or hose pass-through).
    if (tray_hole_diameter > 0) {
        translate([tray_hole_x, tray_hole_y, -1])
            cylinder(h = tray_thickness + 2, d = tray_hole_diameter);
    }
}
