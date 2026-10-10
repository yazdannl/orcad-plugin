// orcad wrapper for rcolyer/smooth-prim (CC0-1.0): SmoothXYCube.
use <smooth-prim-0d0038f9/smooth_prim.scad>

length = 50;
width = 40;
height = 20;
smooth_rad = 6;

SmoothXYCube(size = [length, width, height], smooth_rad = smooth_rad);
