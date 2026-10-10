// orcad wrapper for revarbat/BOSL (BSD-2-Clause): trapezoidal_threaded_rod.
use <bosl-4ce427a8/BOSL/threading.scad>

trap_d = 10;
trap_l = 100;
trap_pitch = 2;
trap_angle = 15;
trap_bevel = false;

trapezoidal_threaded_rod(d = trap_d, l = trap_l, pitch = trap_pitch, thread_angle = trap_angle, bevel = trap_bevel);
