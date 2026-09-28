// orcad: hollow cylinder; validation keeps the wall positive.
outer_diameter = 24;
inner_diameter = 16;
height = 25;

difference() {
    cylinder(d = outer_diameter, h = height);
    translate([0, 0, -1]) cylinder(d = inner_diameter, h = height + 2);
}
