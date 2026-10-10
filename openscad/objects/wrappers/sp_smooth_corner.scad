// orcad wrapper for rcolyer/smooth-prim (CC0-1.0): SmoothCorner().
use <smooth-prim-0d0038f9/smooth_prim.scad>

corner_height = 40;
corner_width = 12;
corner_curv = 0;

SmoothCorner(height = corner_height, width = corner_width, inner_curv = corner_curv);
