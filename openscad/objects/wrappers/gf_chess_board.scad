// orcad wrapper for vector76/gridfinity_openscad (MIT): gridfinity_chess.scad board.
// Upstream picks the part with a string variable, so orcad calls the modules directly. Their
// bases hard-code magnet_diameter=0 and screw_depth=0, so orcad cuts those pockets itself, with
// the sizes and the 13 mm inset grid_block() uses in gridfinity_modules.scad. board() rests its
// 64 tiles 3.55 mm below the pieces, so the pockets are cut at both base levels.
use <gridfinity_openscad-0e7308cd/gridfinity_chess.scad>

magnet_diameter = 0;
screw_depth = 0;

// union() matters: difference() would otherwise subtract the two piece sets from the board.
difference() {
    union() {
        board();
        color("#DDDDDD") piece_set();
        color("#505050") piece_set(false);
    }
    base_pockets([0, -3.55]);
}

// Magnet and screw pockets in the Gridfinity base of every part at *levels*, cut from that
// part's bottom face. The base floors are 5 mm thick, so the pockets never break into the hollow.
module base_pockets(levels) {
    for (z = levels, x = [-1, 1], y = [-1, 1]) {
        if (magnet_diameter > 0)
            translate([x * 13, y * 13, z - 0.1]) cylinder(d = magnet_diameter, h = 2.5, $fn = 41);
        if (screw_depth > 0)
            translate([x * 13, y * 13, z - 0.1]) cylinder(d = 3, h = screw_depth + 0.1, $fn = 28);
    }
}
