// orcad wrapper for revarbat/BOSL (BSD-2-Clause): chamfcube.
use <bosl-4ce427a8/BOSL/shapes.scad>

chamfer_length = 50;
chamfer_width = 30;
chamfer_height = 20;
chamfer_size = 2;

chamfcube(size = [chamfer_length, chamfer_width, chamfer_height], chamfer = chamfer_size);
