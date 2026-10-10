// orcad wrapper for revarbat/BOSL (BSD-2-Clause): lmXuu_housing.
use <bosl-4ce427a8/BOSL/linear_bearings.scad>

bearing_size = 8;
bearing_tab = 7;
bearing_gap = 5;
bearing_wall = 3;
bearing_tabwall = 5;
bearing_screwsize = 3;

lmXuu_housing(size = bearing_size, tab = bearing_tab, gap = bearing_gap, wall = bearing_wall, tabwall = bearing_tabwall, screwsize = bearing_screwsize);
