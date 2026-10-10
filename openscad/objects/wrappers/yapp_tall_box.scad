// orcad wrapper for mrWheel/YAPP_Box (MIT): YAPPgenerator_v3.scad.
// Upstream sizes the whole box from its own top-level assignments and derives many of them
// while the file is read, so orcad exposes the same variable names: the runner's -D values
// arrive before the file computes them. The values below are the fallback when none is given.
pcbLength = 80;
pcbWidth = 50;
pcbThickness = 1.6;
standoffHeight = 1;
wallThickness = 3;
basePlaneThickness = 1.6;
lidPlaneThickness = 1.6;
baseWallHeight = 30;
lidWallHeight = 25;
roundRadius = 4;
include <yapp-box-f9400c41/YAPPgenerator_v3.scad>

showSideBySide = false;
printBaseShell = true;
printLidShell = true;
debug = true;
YAPPgenerate();
