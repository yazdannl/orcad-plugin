// orcad wrapper for scottbez1/splitflap (Apache-2.0): spool.scad, the disc that feeds the flaps.
// Upstream only exposes modules and renders a hard-coded spool at the top level, so orcad
// instantiates flap_spool with the catalog values.
use <splitflap-87b17c53/3d/spool.scad>

spool_flaps = 40;
spool_hole_radius = 1.5;
spool_hole_spacing = 5;
spool_outset = 3;
spool_height = 3.2;

flap_spool(spool_flaps, spool_hole_radius, spool_hole_spacing, spool_outset, spool_height);