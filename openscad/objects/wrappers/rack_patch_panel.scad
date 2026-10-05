// orcad wrapper for jazwa/rackstack (MIT): rack-mount/patch-panel, a blank panel that holds
// blank keystone or cable slots. Upstream takes the slot list as a vector, which -D cannot set,
// so orcad builds the list from a slot count and a slot style.
use <rackstack-8e296e93/rack-mount/patch-panel/patchPanel.scad>

slot_count = 8;
slot_style = 2; // [1:keystone A, 2:keystone B, 3:plate 3 mm, 4:plate 5.9 mm, 5:plate 9.9 mm]
center_slot = 4; // 1-based position of the different slot; 0 gives every slot slot_style
center_slot_style = 5;
plate_thickness = 3;
keystone_spacing = 19;
panel_centered = false;

slot_layout = [for (i = [0 : slot_count - 1])
    (center_slot > 0 && i == center_slot - 1 && center_slot_style > 0)
        ? center_slot_style : slot_style];

mirror(v = [0, 0, 1])
    patchPanel(slots = slot_layout, plateThickness = plate_thickness,
               keystoneSpacing = keystone_spacing, center = panel_centered);