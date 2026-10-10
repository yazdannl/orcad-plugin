// orcad wrapper for Irev-Dev/Round-Anything (MIT): polyround.scad.
use <round-anything-061fef7c/polyround.scad>
size = 80;
height = 16;
length = 8;
radius = 3;

polyRoundExtrude([[-(size - height) / 2, -height / 2, radius],
                 [(size - height) / 2, -height / 2, radius],
                 [(size - height) / 2, height / 2, radius],
                 [-(size - height) / 2, height / 2, radius]],
                 length = length, r1 = radius, r2 = radius);
