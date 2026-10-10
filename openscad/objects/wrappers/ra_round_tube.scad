// orcad wrapper for Irev-Dev/Round-Anything (MIT): polyround.scad.
use <round-anything-061fef7c/polyround.scad>
diameter = 20;
length = 40;
end_radius = 3;

extrudeWithRadius(length = length, r1 = end_radius, r2 = end_radius) circle(d = diameter);
