// orcad wrapper for scottbez1/splitflap (Apache-2.0): flap.scad, one display flap card.
//
// Upstream draws the two character halves of a flap with text(), and it reaches the font settings
// through flap_fonts.scad, which `use`s the Roboto and Epilogue TTF files that upstream ships.
// orcad deliberately does not vendor font binaries, and a missing `use`d file is reported while the
// file is parsed, so `use <flap.scad>` prints "Can't read font" for every render even when the
// letters are switched off - there is no conditional around it. The font-free part of the tree is
// flap_dimensions.scad and global_constants.scad, so this wrapper keeps upstream's dimensions and
// repeats flap_2d() from flap.scad unchanged (the card outline with the spool tabs cut out). That is
// exactly upstream's own blank-card mode: score the card along the fold line, wrap it around the
// spool and add the characters by hand or with a font you supply yourself.
include <splitflap-87b17c53/3d/flap_dimensions.scad>
include <splitflap-87b17c53/3d/global_constants.scad>

flap_quantity = 1;   // how many blank cards to lay out on the bed
flap_spacing = 5;    // clearance between neighbouring cards along the bed

// flap_2d() from upstream flap.scad (Apache-2.0, Copyright 2015-2021 Scott Bezek and the
// splitflap contributors), copied verbatim apart from the added parentheses.
module flap_2d(cut_tabs = true) {
    translate([0, -flap_pin_width/2, 0])
    difference() {
        union() {
            square([flap_width, flap_height - flap_corner_radius]);

            // rounded corners
            hull() {
                translate([flap_corner_radius, flap_height - flap_corner_radius])
                    circle(r = flap_corner_radius, $fn = 40);
                translate([flap_width - flap_corner_radius, flap_height - flap_corner_radius])
                    circle(r = flap_corner_radius, $fn = 40);
            }
        }
        if (cut_tabs) {
            translate([-eps, flap_pin_width])
                square([eps + flap_notch_depth, flap_notch_height]);
            translate([flap_width - flap_notch_depth, flap_pin_width])
                square([eps + flap_notch_depth, flap_notch_height]);
        }
    }
}

// The card body is upstream's flap_2d() translated to z = 0 and extruded by the flap thickness,
// exactly like upstream's _flap(). `use` imports no variables, so flap_thickness comes from the
// include above and only the catalog's own layout values are declared here.
for (i = [0 : flap_quantity - 1])
    translate([0, i * (flap_height + flap_spacing), 0])
        translate([0, 0, -flap_thickness/2])
            linear_extrude(height = flap_thickness)
                flap_2d();
