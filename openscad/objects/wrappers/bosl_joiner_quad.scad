// orcad wrapper for revarbat/BOSL (BSD-2-Clause): joiner_quad.
use <bosl-4ce427a8/BOSL/joiners.scad>

quad_spacing_x = 120;
quad_spacing_y = 80;
quad_count = 2;
quad_height = 60;
quad_width = 12;
quad_depth = 10;
quad_angle = 30;

joiner_quad(xspacing = quad_spacing_x, yspacing = quad_spacing_y, n = quad_count, h = quad_height, w = quad_width, l = quad_depth, a = quad_angle);
