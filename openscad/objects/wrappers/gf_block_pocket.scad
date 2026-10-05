// orcad wrapper for wromijn/openscad-gridfinity-block (Apache-2.0): a Gridfinity block
// with round pockets, which is what the library's own coordinate helpers are for.
grid_x = 2;
grid_y = 1;
grid_z = 3;
pocket_diameter = 14.5;
pocket_depth = 30;
pocket_spacing = 17;
stacking_lip = true;
magnets = true;

use <openscad-gridfinity-block-6ef6d644/gridfinity_block.scad>

gridfinity_block([grid_x, grid_y, grid_z], stacking_lip, true, magnets) {
    for (x = [0 : grid_x - 1]) {
        gb_round_hole([ (x - (grid_x - 1) / 2) * pocket_spacing, 0 ], pocket_diameter, pocket_depth) {
            cylinder(h = $inner_height, d = pocket_diameter - 0.6);
        }
    }
}
