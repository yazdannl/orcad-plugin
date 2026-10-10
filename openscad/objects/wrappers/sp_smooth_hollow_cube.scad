// orcad wrapper for rcolyer/smooth-prim (CC0-1.0): SmoothHollowCube.
use <smooth-prim-0d0038f9/smooth_prim.scad>

length = 60;
width = 40;
height = 30;
wall = 3;
inner_curv = 1;

SmoothHollowCube(size = [length, width, height], wall_width = wall, inner_curv = inner_curv);
