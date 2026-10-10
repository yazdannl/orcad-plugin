// orcad wrapper for rcolyer/smooth-prim (CC0-1.0): SmoothCube.
use <smooth-prim-0d0038f9/smooth_prim.scad>

length = 40;
width = 40;
height = 40;
smooth_rad = 5;

SmoothCube(size = [length, width, height], smooth_rad = smooth_rad);
