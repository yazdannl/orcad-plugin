// orcad: rectangular block, optionally with rounded vertical edges.
length = 20;
width = 20;
height = 20;
corner_radius = 0;

r = min(corner_radius, length / 2 - 0.01, width / 2 - 0.01);
if (r > 0) {
    linear_extrude(height)
        offset(r = r) square([length - 2 * r, width - 2 * r], center = true);
} else {
    translate([-length / 2, -width / 2, 0]) cube([length, width, height]);
}
