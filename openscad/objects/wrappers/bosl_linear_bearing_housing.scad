// orcad wrapper for revarbat/BOSL (BSD-2-Clause): linear_bearing_housing.
use <bosl-4ce427a8/BOSL/linear_bearings.scad>

bearing_d = 15;
bearing_l = 24;
bearing_tab = 8;
bearing_gap = 5;
bearing_wall = 3;
bearing_tabwall = 5;
bearing_screwsize = 3;

linear_bearing_housing(d = bearing_d, l = bearing_l, tab = bearing_tab, gap = bearing_gap, wall = bearing_wall, tabwall = bearing_tabwall, screwsize = bearing_screwsize);
