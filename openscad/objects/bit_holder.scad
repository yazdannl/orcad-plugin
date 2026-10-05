// orcad: block that stands a row of drill bits or hex driver bits upright in bored holes.
hole_style = 0; // [0:Round, 1:Hex flat]
bit_diameter = 6.35;
column_count = 4;
row_count = 1;
hole_spacing = 12;
hole_depth = 25;
base_thickness = 4;
edge_margin = 6;
hole_tolerance = 0.2;
chamfer = 0.6;
corner_radius = 3;

col_n = max(column_count, 1);
row_n = max(row_count, 1);
hole_d = bit_diameter + 2 * hole_tolerance;       // across the flats of a hex hole
corners = hole_d * 2 / sqrt(3);                   // across the corners of a hex hole
chamfer_h = min(chamfer, max(hole_depth - 1, 0));
block_x = max((col_n - 1) * hole_spacing + corners + 2 * edge_margin, corners + 4);
block_y = max((row_n - 1) * hole_spacing + corners + 2 * edge_margin, corners + 4);
block_height = hole_depth + base_thickness;
r = min(corner_radius, block_x / 2 - 0.01, block_y / 2 - 0.01);

module bit_hole() {
    if (hole_style == 1) {
        cylinder(d = corners, h = hole_depth + 0.01, $fn = 6);
        if (chamfer_h > 0)
            translate([0, 0, hole_depth - chamfer_h])
                cylinder(d1 = corners + 2 * chamfer_h, d2 = corners, h = chamfer_h + 0.01, $fn = 6);
    } else {
        cylinder(d = hole_d, h = hole_depth + 0.01);
        if (chamfer_h > 0)
            translate([0, 0, hole_depth - chamfer_h])
                cylinder(d1 = hole_d + 2 * chamfer_h, d2 = hole_d, h = chamfer_h + 0.01);
    }
}

difference() {
    translate([-block_x / 2 + r, -block_y / 2 + r, 0])
        if (r > 0)
            linear_extrude(height = block_height)
                offset(r = r) square([block_x - 2 * r, block_y - 2 * r]);
        else
            cube([block_x, block_y, block_height]);

    for (i = [0 : col_n - 1], j = [0 : row_n - 1])
        translate([(i - (col_n - 1) / 2) * hole_spacing, (j - (row_n - 1) / 2) * hole_spacing, base_thickness])
            bit_hole();
}
