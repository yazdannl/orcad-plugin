// orcad wrapper for eclecticc/ParametricCase (BSD-2-Clause): front_panel.scad.
use <param-case-b5f1ee43/front_panel.scad>

panel_width = 80;
panel_height = 60;
panel_thickness = 3;
cutout_gap = 4;

difference() {
    cube([panel_width, panel_height, panel_thickness]);
    translate([panel_width / 2 - cutout_gap / 2, panel_height / 2, 0])
        dual_usb_cutout();
    translate([panel_width / 2 + cutout_gap / 2, panel_height / 2, 0])
        dual_usb_cutout();
}
