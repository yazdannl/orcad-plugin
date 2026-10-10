// orcad wrapper for revarbat/BOSL (BSD-2-Clause): threaded_nut.
use <bosl-4ce427a8/BOSL/threading.scad>

nut_od = 16;
nut_id = 8;
nut_h = 8;
nut_pitch = 1.25;
nut_bevel = false;

threaded_nut(od = nut_od, id = nut_id, h = nut_h, pitch = nut_pitch, bevel = nut_bevel);
