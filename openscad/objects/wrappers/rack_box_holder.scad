// orcad wrapper for jazwa/rackstack (MIT): rack-mount/catalog/beelink-EQi12-box_mod.scad.
use <rackstack-8e296e93/rack-mount/catalog/beelink-EQi12-box_mod.scad>

box_width = 126;
box_height = 44.5;
box_depth = 126;
rail_thickness = 1.5;
rail_side_thickness = 3;
plate_thickness = 3;
plate_cutout_x = 5;
plate_cutout_y = 3;

enclosedBoxSystem(visualize = false,
                  boxWidth = box_width,
                  boxHeight = box_height,
                  boxDepth = box_depth,
                  railDefaultThickness = rail_thickness,
                  railSideThickness = rail_side_thickness,
                  frontPlateThickness = plate_thickness,
                  frontPlateCutoutXSpace = plate_cutout_x,
                  frontPlateCutoutYSpace = plate_cutout_y);
