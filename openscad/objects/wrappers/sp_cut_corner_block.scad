// orcad wrapper for rcolyer/smooth-prim (CC0-1.0): CutCorner().
use <smooth-prim-0d0038f9/smooth_prim.scad>

block_length = 60;
block_width = 60;
block_height = 30;
cut_x = 20;
cut_y = 20;
cut_z = 20;

difference() {
    cube([block_length, block_width, block_height]);
    CutCorner(insetby = cut_x, cornerpos = [1, 0, 0], positives = [cut_x, 1, 1]);
    CutCorner(insetby = cut_y, cornerpos = [0, 1, 0], positives = [1, cut_y, 1]);
    CutCorner(insetby = cut_z, cornerpos = [0, 0, 1], positives = [1, 1, cut_z]);
}
