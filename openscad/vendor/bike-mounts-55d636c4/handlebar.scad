use <catchnhole/catchnhole.scad>;

DEFAULT_DIAMETER = 23.5;  // Really hand-wavy default. We aim to be oversized for most handlebars.
DEFAULT_THICKNESS = 4;  // Manufacturing-driven default. Get some layers in there, son.

module handlebar_mount (
  width,
  diameter = DEFAULT_DIAMETER,
  angle = 320,            // Reasonable default.
  bolt_size = "M5",
  bolt_kind = "socket_head",
  bolt_countersink = 0.5,
  bolt_mount_length = 14, // Reasonable default for an M5 bolt.
  thickness = DEFAULT_THICKNESS,
  top_offset = 0,         // Cut this much off the top (screw side), making part of the top flat. Only makes sense if angle > 270.
  // XXX(mmalecki): center_offset - cut this much off the center (usecase: flashlight mounting)
  // XXX(mmalecki): bottom_offset - cut this much off the bottom (usecase: uhh?)
) {
  rotate_facing = (360 - angle) / 2;

  difference () {
    // This final rotate is ugly, but convenient for actually developing this.
    rotate ([90, -rotate_facing + 180, 90]) {
      difference () {
        union () {
          rotate_extrude (angle = angle) {
            translate([diameter / 2, 0]) square([thickness, width]);
          }

          translate([diameter / 2, 0]) {
            rotate([90, 0, -rotate_facing]) {
              translate([0, 0, -thickness])
                cube([bolt_mount_length, width, thickness]);
            }
          }

          rotate([0, 0, angle]) {
            translate([diameter / 2, 0]) {
              rotate([90, 0, rotate_facing]) {
                cube([bolt_mount_length, width, thickness]);
              }
            }
          }
        }

        translate([diameter / 2, 0]) {
          rotate([90, 0, -rotate_facing]) {
            translate([0, 0, -thickness]) {
              translate([bolt_mount_length / 2 + thickness / 4, width / 2, 0]){
                bolt(bolt_size,
                  kind = bolt_kind,
                  countersink = bolt_countersink,
                  length = sin((360 - angle) / 2) * diameter + 2 * thickness
                );
                nutcatch_parallel(bolt_size);
              }
            }
          }
        }

      }
    }

    if (top_offset > 0) {
      translate([0, -diameter / 2, diameter / 2 + thickness - top_offset])
        cube([width, diameter, top_offset]);
    }
  }
}

module to_handlebar_mount_top(
  diameter = DEFAULT_DIAMETER,
  thickness = DEFAULT_THICKNESS,
) {
  translate([0, 0, diameter / 2 + thickness]) children();
}
