// orcad wrapper for rcolyer/threads-scad (CC0): ScrewThread, a fully threaded rod.
use <threads-scad-4ae9aeb3/threads.scad>

rod_diameter = 6;
rod_length = 40;
rod_flat_length = 6;
rod_tolerance = 0.4;

translate([0, 0, rod_flat_length])
    ScrewThread(rod_diameter, rod_length, tolerance = rod_tolerance, tip_height = rod_flat_length);
