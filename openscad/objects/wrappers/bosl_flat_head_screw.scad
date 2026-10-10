// orcad wrapper for revarbat/BOSL (BSD-2-Clause): screw.
use <bosl-4ce427a8/BOSL/metric_screws.scad>

flat_size = 3;
flat_length = 12;
flat_pitch = 0;

screw(headtype = "flat", size = flat_size, l = flat_length, pitch = flat_pitch);
