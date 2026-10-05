// orcad wrapper for jazwa/rackstack (MIT): rack-mount/tray/tray.scad.
// Same idea as rack-mount/catalog/120mm-fan-tray_mod.scad, except upstream hard-codes every tray
// and fan dimension. orcad builds the mount-point grid from the tray size and centers the fan
// cutout and its four screw holes on the tray, so the tray works for any case fan between
// fan_screw_spacing and fan_cutout_diameter across the tray.
fan_tray_u = 2;                 // rack units (1 U = 10 mm with the default rack profile)
fan_tray_width = 140;           // tray width, must stay within the rack opening
fan_tray_depth = 140;           // tray depth
fan_tray_thickness = 3;         // tray floor thickness
fan_tray_front_lip = 26;        // lip height at the front, keeps the fan from sliding out
fan_tray_back_lip = 6;
fan_tray_padding = 15;          // distance from the tray edge to the screw stand-off
fan_cutout_diameter = 120;      // fan opening through the tray floor
fan_screw_spacing = 105;        // distance between opposite fan screw holes
fan_screw_diameter = 5;         // fan screw hole diameter

use <rackstack-8e296e93/rack-mount/tray/tray.scad>

difference() {
    bottomScrewTray(
        u = fan_tray_u,
        trayWidth = fan_tray_width,
        trayDepth = fan_tray_depth,
        trayThickness = fan_tray_thickness,
        frontLipHeight = fan_tray_front_lip,
        backLipHeight = fan_tray_back_lip,
        mountPoints = [
            [fan_tray_padding, fan_tray_padding],
            [fan_tray_width - fan_tray_padding, fan_tray_padding],
            [fan_tray_padding, fan_tray_depth - fan_tray_padding],
            [fan_tray_width - fan_tray_padding, fan_tray_depth - fan_tray_padding]
        ],
        frontThickness = 3,
        sideThickness = 3,
        mountPointElevation = 1,
        mountPointType = "m4",
        sideSupport = true,
        trayLeftPadding = fan_tray_padding
    );

    translate([fan_tray_width / 2, fan_tray_depth / 2, -1])
        cylinder(h = fan_tray_thickness + 2, d = fan_cutout_diameter);

    for (sx = [-1, 1], sy = [-1, 1])
        translate([fan_tray_width / 2 + sx * fan_screw_spacing / 2,
                   fan_tray_depth / 2 + sy * fan_screw_spacing / 2, -1])
            cylinder(h = fan_tray_thickness + 2, d = fan_screw_diameter);
}
