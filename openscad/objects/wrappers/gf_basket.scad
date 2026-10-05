// orcad wrapper for LeKoYa/gridfinity-basket-openscad (MIT): gridfinityBasket.scad.
// Upstream sizes the basket from a GridSize vector, so orcad passes the three units
// separately and rebuilds the vector after the include.
grid_x = 3;
grid_y = 1;
grid_z = 4;
basket_wall_thickness = 1.2;
basket_padding = 1;
basket_wall_pattern = 1; // [0:solid, 1:hex grid, 2:grid]
basket_pattern_size = 8;
basket_handle = true;
basket_handle_width = 35;
basket_handle_height = 11;
basket_solid_floor = true;
basket_gridfinity_base = true;

include <gridfinity-basket-openscad-549dc401/gridfinityBasket.scad>

GridSize = [grid_x, grid_y, grid_z];
WallThickness = basket_wall_thickness;
Padding = basket_padding;
WallPattern = basket_wall_pattern;
PatternSize = basket_pattern_size;
AddHandle = basket_handle;
HandleWidth = basket_handle_width;
HandleHeight = basket_handle_height;
SolidFloor = basket_solid_floor;
UseGridfinityBase = basket_gridfinity_base;