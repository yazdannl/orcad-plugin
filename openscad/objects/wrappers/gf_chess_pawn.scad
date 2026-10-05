// orcad wrapper for vector76/gridfinity_openscad (MIT): gridfinity_chess.scad pawn.
// Upstream selects the piece with a string variable, so orcad calls the module directly. Its
// base() hard-codes magnet_diameter=0 and screw_depth=0, so orcad cuts those pockets itself,
// with the sizes and the 13 mm inset grid_block() uses in gridfinity_modules.scad.
use <gridfinity_openscad-0e7308cd/gridfinity_chess.scad>

magnet_diameter = 0;
screw_depth = 0;

difference() {
    pawn();
    base_pockets([0]);
}

// Magnet and screw pockets in the Gridfinity base of every part at *levels*, cut from that
// part's bottom face. The base floor is 5 mm thick, so the pockets never break into the hollow.
module base_pockets(levels) {
    for (z = levels, x = [-1, 1], y = [-1, 1]) {
        if (magnet_diameter > 0)
            translate([x * 13, y * 13, z - 0.1]) cylinder(d = magnet_diameter, h = 2.5, $fn = 41);
        if (screw_depth > 0)
            translate([x * 13, y * 13, z - 0.1]) cylinder(d = 3, h = screw_depth + 0.1, $fn = 28);
    }
}
