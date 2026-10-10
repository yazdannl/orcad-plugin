// orcad wrapper for revarbat/BOSL (BSD-2-Clause): threaded_rod.
use <bosl-4ce427a8/BOSL/threading.scad>

rod_d = 8;
rod_l = 60;
rod_pitch = 1.25;
rod_bevel = false;
rod_left_handed = false;

threaded_rod(d = rod_d, l = rod_l, pitch = rod_pitch, bevel = rod_bevel, left_handed = rod_left_handed);
