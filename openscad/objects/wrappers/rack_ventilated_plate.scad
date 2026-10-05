// orcad wrapper for jazwa/rackstack (MIT): rack-mount/catalog/ventilated_plate.scad.
// That file renders three example plates at the top level, so orcad calls its module with
// catalog values instead of including the file.
plate_u = 1;
plate_thickness = 3;
slot_width = 2;
slot_spacing = 4;
border_x = 10;
border_y = 3;
corner_radius = 2;

use <rackstack-8e296e93/rack-mount/catalog/ventilated_plate.scad>

ventilatedPlate(U = plate_u, plateThickness = plate_thickness, screwType = "m4",
                filletR = corner_radius, slotWidth = slot_width, spacing = slot_spacing,
                borderX = border_x, borderY = border_y);
