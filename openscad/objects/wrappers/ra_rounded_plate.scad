// orcad wrapper for Irev-Dev/Round-Anything (MIT): polyround.scad.
use <round-anything-061fef7c/polyround.scad>
size = 80;
height = 50;
length = 4;
radius = 10;

polyRoundExtrude([[radius, radius, radius], [size - radius, radius, radius],
                [size - radius, height - radius, radius], [radius, height - radius, radius]],
                length = length, r1 = radius, r2 = radius);
