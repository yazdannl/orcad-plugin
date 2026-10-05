// orcad: flat wall plate with a grid of screw holes, rounded corners and optional countersinks.
plate_width = 60;
plate_height = 60;
plate_thickness = 4;
columns = 2;
rows = 2;
hole_spacing_x = 40;
hole_spacing_y = 40;
hole_diameter = 4;
countersink = true;
corner_radius = 6;
sink_diameter = 8;

col_n = max(columns, 1);
row_n = max(rows, 1);
sink_d = max(sink_diameter, hole_diameter + 1);
half_x = (plate_width - sink_d) / 2 - corner_radius;
half_y = (plate_height - sink_d) / 2 - corner_radius;
spacing_x = min(hole_spacing_x, max(2 * half_x, 0));
spacing_y = min(hole_spacing_y, max(2 * half_y, 0));
sink_depth = min((sink_d - hole_diameter) / 2, plate_thickness * 0.8);
r = min(corner_radius, plate_width / 2 - 0.01, plate_height / 2 - 0.01);

difference() {
    if (r > 0)
        linear_extrude(height = plate_thickness)
            offset(r = r) square([plate_width - 2 * r, plate_height - 2 * r], center = true);
    else
        cube([plate_width, plate_height, plate_thickness], center = true);

    for (i = [0 : col_n - 1], j = [0 : row_n - 1])
        translate([(i - (col_n - 1) / 2) * spacing_x, (j - (row_n - 1) / 2) * spacing_y, -0.01])
            cylinder(d = hole_diameter, h = plate_thickness + 0.02);

    if (countersink)
        for (i = [0 : col_n - 1], j = [0 : row_n - 1])
            translate([(i - (col_n - 1) / 2) * spacing_x, (j - (row_n - 1) / 2) * spacing_y,
                       plate_thickness - sink_depth])
                cylinder(d1 = sink_d, d2 = hole_diameter, h = sink_depth + 0.01);
}
