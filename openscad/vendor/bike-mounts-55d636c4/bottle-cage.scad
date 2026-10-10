use <catchnhole/catchnhole.scad>;

module bottle_cage_mount (
  bolt_length,
  bolt_size = "M5",
  bolt_kind = "socket_head",

  // As per Wikipedia: https://en.wikipedia.org/wiki/Bottle_cage
  bolt_distance_spacing = 64,

  // Leave ~15 % tolerance for you filthy animals with non-standard spaced bolts.
  bolt_distance_tolerance = 10,

  // These bolts tend to have larger heads to hold better.
  bolt_head_clearance = 2,

  // Default for M5 bolts.
  bolt_head_top_clearance = 5, 
) {
  // The first bolt.
  bolt(
    bolt_size,
    kind = bolt_kind,
    length = bolt_length,
    head_diameter_clearance = bolt_head_clearance,
    head_top_clearance = bolt_head_top_clearance,
  );

  // And because life is never easy, we have to hull the actual bolts
  // and bolt heads separately.
  translate([bolt_distance_spacing, 0]) {
    hull () {
      translate([-bolt_distance_tolerance / 2, 0])
        bolt(
          bolt_size,
          kind = "headless",
          length = bolt_length,
        );

      translate([bolt_distance_tolerance / 2, 0])
        bolt(
          bolt_size,
          kind = "headless",
          length = bolt_length,
        );
    }

    translate([0, 0, bolt_length]) {
      hull () {
        translate([-bolt_distance_tolerance / 2, 0])
          bolt_head(
            bolt_size,
            kind = bolt_kind,
            head_diameter_clearance = bolt_head_clearance,
            head_top_clearance = bolt_head_top_clearance,
          );

        translate([bolt_distance_tolerance / 2, 0])
          bolt_head(
            bolt_size,
            kind = bolt_kind,
            head_diameter_clearance = bolt_head_clearance,
            head_top_clearance = bolt_head_top_clearance,
          );
      }
    }
  }
}

$fn=50;
difference () {
  cube([100, 20, 5]);
  translate([10, 10]) bottle_cage_mount(3);
}
