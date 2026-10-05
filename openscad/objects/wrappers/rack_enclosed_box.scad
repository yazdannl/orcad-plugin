// orcad wrapper for jazwa/rackstack (MIT): rack-mount/enclosed-box, an open box that clips
// between the rack rails. Upstream's entry file only calls its module with the defaults, and
// zOrientation is a string that -D cannot set, so orcad passes integers and maps them to the
// strings upstream expects.
use <rackstack-8e296e93/rack-mount/enclosed-box/entry.scad>

box_width = 160;
box_height = 27;
box_depth = 120;
rail_thickness = 1.5;
rail_side_thickness = 3;
front_plate_thickness = 3;
recess_side_rail = false;
z_orientation = 0; // [0:middle, 1:bottom]
z_orientation_names = ["middle", "bottom"];

enclosedBoxSystem(visualize = false, zOrientation = z_orientation_names[z_orientation],
                  recessSideRail = recess_side_rail, boxWidth = box_width, boxHeight = box_height,
                  boxDepth = box_depth, railDefaultThickness = rail_thickness,
                  railSideThickness = rail_side_thickness,
                  frontPlateThickness = front_plate_thickness);