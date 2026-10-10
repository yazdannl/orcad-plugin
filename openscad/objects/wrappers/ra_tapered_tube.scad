// orcad wrapper for Irev-Dev/Round-Anything (MIT): polyround.scad.
use <round-anything-061fef7c/polyround.scad>
diameter = 30;
length = 40;
top_radius = 5;
bottom_radius = 2;

extrudeWithRadius(length = length, r1 = top_radius, r2 = bottom_radius) circle(d = diameter);
