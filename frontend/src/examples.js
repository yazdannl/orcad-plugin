// Code-mode starters. `include <src/...>` resolves against the bundled
// Gridfinity Rebuilt library, so its modules are available in your own code.
export const EXAMPLES = {
  rounded_box: {
    label: 'Rounded storage box',
    code: `// Rounded open box. Edit the numbers and press Render (Ctrl+Enter).
width = 60;
depth = 40;
height = 30;
wall = 2;
radius = 5;

module rounded(w, d, h, r) {
    linear_extrude(h) offset(r = r) square([w - 2 * r, d - 2 * r], center = true);
}

difference() {
    rounded(width, depth, height, radius);
    translate([0, 0, wall]) rounded(width - 2 * wall, depth - 2 * wall, height, radius - wall);
}
`,
  },
  gridfinity_pockets: {
    label: 'Gridfinity bin with round pockets',
    code: `// Uses the bundled Gridfinity Rebuilt library.
include <src/core/standard.scad>
use <src/core/gridfinity-rebuilt-utility.scad>
use <src/core/gridfinity-rebuilt-holes.scad>
use <src/core/bin.scad>
use <src/core/cutouts.scad>
use <src/helpers/grid.scad>
use <src/helpers/grid_element.scad>

pockets = [3, 1];      // columns, rows
pocket_diameter = 16;

bin = new_bin([2, 1], fromGridfinityUnits(3));
bin_render(bin) {
    bin_subdivide(bin, pockets) {
        cut_chamfered_cylinder(pocket_diameter / 2, bin_get_infill_size_mm(bin).z);
    }
}
`,
  },
  cable_clip: {
    label: 'Desk cable clip',
    code: `// Screw-mount clip for a bundle of cables.
cable_d = 8;
wall = 2.4;
width = 12;
screw_d = 3.5;

difference() {
    union() {
        cylinder(d = cable_d + 2 * wall, h = width, $fn = 64);
        translate([0, -(cable_d / 2 + wall), 0]) cube([cable_d + 10, wall, width]);
    }
    translate([0, 0, -1]) cylinder(d = cable_d, h = width + 2, $fn = 64);
    translate([0, 0, -1]) cube([cable_d, cable_d, width + 2]);
    translate([cable_d / 2 + 6, 0, width / 2]) rotate([90, 0, 0]) cylinder(d = screw_d, h = 20, $fn = 24);
}
`,
  },
}

export const DEFAULT_EXAMPLE = 'rounded_box'
