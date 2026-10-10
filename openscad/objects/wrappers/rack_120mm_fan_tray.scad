// orcad wrapper for jazwa/rackstack (MIT): rack-mount/catalog/120mm-fan-tray_mod.scad.
use <rackstack-8e296e93/rack-mount/tray/tray.scad>

tray_u = 2;
tray_width = 140;
tray_depth = 140;
tray_thickness = 3;
front_lip = 26;
back_lip = 6;
tray_padding = 15;
fan_opening = 120;
fan_spacing = 105;
fan_hole = 5;

difference() {
    bottomScrewTray(
        u = tray_u,
        trayWidth = tray_width,
        trayDepth = tray_depth,
        trayThickness = tray_thickness,
        frontLipHeight = front_lip,
        backLipHeight = back_lip,
        mountPoints = [[tray_padding, tray_padding],
                       [tray_width - tray_padding, tray_padding],
                       [tray_padding, tray_depth - tray_padding],
                       [tray_width - tray_padding, tray_depth - tray_padding]],
        frontThickness = tray_thickness,
        sideThickness = tray_thickness,
        mountPointElevation = 1,
        mountPointType = "m4",
        sideSupport = true,
        trayLeftPadding = tray_padding
    );

    translate([tray_width / 2, tray_depth / 2, -1])
        cylinder(h = tray_thickness + 2, d = fan_opening);

    for (s = [[-1, -1], [-1, 1], [1, -1], [1, 1]])
        translate([tray_width / 2 + s[0] * fan_spacing / 2,
                   tray_depth / 2 + s[1] * fan_spacing / 2, -1])
            cylinder(h = tray_thickness + 2, d = fan_hole);
}
