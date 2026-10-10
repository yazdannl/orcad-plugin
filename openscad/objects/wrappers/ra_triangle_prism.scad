// orcad wrapper for Irev-Dev/Round-Anything (MIT): polyround.scad.
use <round-anything-061fef7c/polyround.scad>
size = 60;
length = 30;
radius = 6;

polyRoundExtrude([[0, 0, radius], [size, 0, radius], [size / 2, size * 0.866, radius]], length = length, r1 = radius, r2 = radius);
