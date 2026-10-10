// orcad wrapper for revarbat/BOSL (BSD-2-Clause): acme_threaded_rod.
use <bosl-4ce427a8/BOSL/threading.scad>

acme_d = 10;
acme_l = 100;
acme_pitch = 3;
acme_bevel = false;

acme_threaded_rod(d = acme_d, l = acme_l, pitch = acme_pitch, bevel = acme_bevel);
