// orcad wrapper for mmalecki/openscad-bicycle-mounts (MIT): handlebar_mount().
use <bike-mounts-55d636c4/handlebar.scad>

mount_width = 20;
bar_diameter = 23.5;
wrap_angle = 320;
bolt_length = 14;
mount_thickness = 4;
top_offset = 0;
bolt_size = "M5";
bolt_kind = "socket_head";
bolt_countersink = 0.5;

handlebar_mount(mount_width, diameter = bar_diameter, angle = wrap_angle,
                bolt_size = bolt_size, bolt_kind = bolt_kind,
                bolt_countersink = bolt_countersink, bolt_mount_length = bolt_length,
                thickness = mount_thickness, top_offset = top_offset);
