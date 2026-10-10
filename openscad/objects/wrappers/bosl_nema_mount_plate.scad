// orcad wrapper for revarbat/BOSL (BSD-2-Clause): nema_mount_holes().
// The upstream module only cuts the mounting holes, so the wrapper cuts them out of a plate.
use <bosl-4ce427a8/BOSL/nema_steppers.scad>

nema_size = 17;
nema_depth = 4;
nema_length = 5;
plate_width = 60;
plate_depth = 60;
plate_thickness = 4;

difference() {
    cube([plate_width, plate_depth, plate_thickness]);
    nema_mount_holes(size = nema_size, depth = nema_depth, l = nema_length);
}
