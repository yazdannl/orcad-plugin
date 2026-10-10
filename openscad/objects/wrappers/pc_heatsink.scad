// orcad wrapper for eclecticc/ParametricCase (BSD-2-Clause): heatsink().
use <param-case-b5f1ee43/heatsink.scad>

sink_length = 37;
sink_width = 37;
sink_height = 30;
sink_base = 2;
sink_fins = 20;

heatsink(size = [sink_length, sink_width, sink_height], base = sink_base, fins = sink_fins);
