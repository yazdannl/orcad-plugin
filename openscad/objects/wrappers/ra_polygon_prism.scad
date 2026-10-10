// orcad wrapper for Irev-Dev/Round-Anything (MIT): polyround.scad.
use <round-anything-061fef7c/polyround.scad>
sides = 5;
radius_corner = 30;
length = 20;
radius = 2;

polyRoundExtrude([for (i = [0:sides - 1])
                      [radius_corner * cos(360 * i / sides),
                       radius_corner * sin(360 * i / sides), radius]],
                 length = length, r1 = radius, r2 = radius);
