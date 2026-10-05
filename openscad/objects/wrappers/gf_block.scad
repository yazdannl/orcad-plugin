// orcad wrapper for wromijn/openscad-gridfinity-block (Apache-2.0): gridfinity_block.scad.
// The upstream library only exposes modules, so orcad fixes the block size here.
grid_x = 2;
grid_y = 1;
grid_z = 3;
stacking_lip = true;
center = true;
magnets = true;

use <openscad-gridfinity-block-6ef6d644/gridfinity_block.scad>

gridfinity_block([grid_x, grid_y, grid_z], stacking_lip, center, magnets);
