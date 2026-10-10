// orcad wrapper for Irev-Dev/Round-Anything (MIT): polyround.scad.
use <round-anything-061fef7c/polyround.scad>
size = 50;
length = 12;
radius = 3;

polyRoundExtrude([for (a = [0, 60, 120, 180, 240, 300])
                      [size * 0.866 * cos(a), size * 0.866 * sin(a), radius]],
                 length = length, r1 = radius, r2 = radius);
