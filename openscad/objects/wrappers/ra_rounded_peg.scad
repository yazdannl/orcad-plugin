// orcad wrapper for Irev-Dev/Round-Anything (MIT): MinkowskiRound.scad.
use <round-anything-061fef7c/MinkowskiRound.scad>
diameter = 16;
length = 30;
end_radius = 2;

minkowskiOutsideRound(r = end_radius) cylinder(d = diameter, h = length);
