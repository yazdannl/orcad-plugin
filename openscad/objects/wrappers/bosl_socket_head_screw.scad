// orcad wrapper for revarbat/BOSL (BSD-2-Clause): screw.
use <bosl-4ce427a8/BOSL/metric_screws.scad>

screw_size = 3;
screw_length = 16;
screw_pitch = 0;

screw(headtype = "socket", size = screw_size, l = screw_length, pitch = screw_pitch);
