// orcad wrapper for revarbat/BOSL (BSD-2-Clause): cuboid.
use <bosl-4ce427a8/BOSL/shapes.scad>

block_length = 60;
block_width = 40;
block_height = 20;
block_fillet = 5;

cuboid(size = [block_length, block_width, block_height], fillet = block_fillet);
