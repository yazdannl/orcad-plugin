// orcad: tray that holds a row of cells (AA, AAA, 18650 or a 9 V block battery) in pockets.
cell_type = 0; // [0:AA, 1:AAA, 2:18650, 3:9V block]
cell_count = 4;
cell_clearance = 0.7;
pocket_depth = 32;
floor_thickness = 2;
wall_height = 8;
wall_thickness = 2;

// Nominal cell width and length in mm: AA, AAA, 18650 and a 9 V block lying flat.
cells = [[14.5, 50.5], [10.5, 44.5], [18.6, 65], [26.5, 48.5]];
block_cell = cell_type == 3;
cell_width = cells[cell_type][0];
cell_length = cells[cell_type][1];

pocket_x = (block_cell ? cell_length : cell_width) + 2 * cell_clearance;
pocket_y = cell_width + 2 * cell_clearance;
base_x = cell_count * pocket_x + (cell_count + 1) * wall_thickness;
base_y = pocket_y + 2 * wall_thickness;
base_height = floor_thickness + pocket_depth;

difference() {
    union() {
        cube([base_x, base_y, base_height]);

        // Rim around the tray, so the cells cannot fall out.
        translate([0, 0, base_height])
            difference() {
                cube([base_x, base_y, wall_height]);
                translate([wall_thickness, wall_thickness, -0.01])
                    cube([base_x - 2 * wall_thickness, base_y - 2 * wall_thickness, wall_height + 0.02]);
            }
    }

    for (i = [0 : cell_count - 1])
        translate([wall_thickness + i * (pocket_x + wall_thickness) + pocket_x / 2, base_y / 2, 0])
            if (block_cell)
                translate([-pocket_x / 2, -pocket_y / 2, floor_thickness])
                    cube([pocket_x, pocket_y, pocket_depth]);
            else
                translate([0, 0, floor_thickness]) cylinder(d = pocket_x, h = pocket_depth);
}