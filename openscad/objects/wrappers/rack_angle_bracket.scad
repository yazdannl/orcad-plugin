// orcad wrapper for jazwa/rackstack (MIT): rack-mount/angle-bracket, a pair of rails that
// carry a box without a front plate. Upstream's entry file adds a visualization cube for the
// box; orcad leaves that out so the file stays a single printable part.
include <rackstack-8e296e93/rack-mount/common.scad>
use <rackstack-8e296e93/rack-mount/enclosed-box/sideRail.scad>

bracket_u = 3;
bracket_thickness = 3;
bracket_width = 160;
bracket_depth = 120;
bracket_vent = false;

for (x = [0, 1])
    translate(v = [x * 30, 0, 0]) mirror(v = [x, 0, 0])
        sideSupportRailBase(top = false, defaultThickness = bracket_thickness,
                            railSideThickness = bracket_thickness,
                            supportedZ = 10 * bracket_u - 2 * bracket_thickness,
                            supportedY = bracket_depth, supportedX = bracket_width,
                            sideVent = bracket_vent);