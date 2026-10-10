// orcad wrapper for Irev-Dev/Round-Anything (MIT): polyround.scad.
use <round-anything-061fef7c/polyround.scad>
size = 40;
height = 60;
length = 6;
radius = 4;

polyRoundExtrude([[0, 0, radius], [size, 0, radius], [size, height, 0], [0, height, 0], [0, height / 2, height / 2]], length = length, r1 = radius, r2 = radius);
