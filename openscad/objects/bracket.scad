// orcad: flat mounting plate with two through holes.
length = 60;
width = 30;
thickness = 5;
hole_diameter = 5;
hole_spacing = 30;
corner_radius = 3;

r = min(corner_radius, length / 2 - 0.01, width / 2 - 0.01);
difference() {
    linear_extrude(thickness)
        offset(r = r) square([length - 2 * r, width - 2 * r], center = true);
    for (x = [-hole_spacing / 2, hole_spacing / 2])
        translate([x, 0, -1]) cylinder(d = hole_diameter, h = thickness + 2);
}
