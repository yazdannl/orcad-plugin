// orcad wrapper for revarbat/BOSL (BSD-2-Clause): acme_threaded_nut.
use <bosl-4ce427a8/BOSL/threading.scad>

acme_nut_od = 18;
acme_nut_id = 10;
acme_nut_h = 12;
acme_nut_pitch = 3;
acme_nut_bevel = false;

acme_threaded_nut(od = acme_nut_od, id = acme_nut_id, h = acme_nut_h, pitch = acme_nut_pitch, bevel = acme_nut_bevel);
