# object: Gridfinity Baseplate
# blurb: Grid the bins snap into, with sockets + magnet holes.
# Thin-style slab port of kennetek/gridfinity-rebuilt-openscad
# gridfinity-rebuilt-baseplate.scad: slab GX*42 x GY*42, one tapered socket
# per cell, four 6x2 magnet holes per cell. Hole shapes mirror the bin
# builder (block_base_hole), flipped to open at the top surface.
# Full plate styles (weighted/skeletonized/screw-together/fit-to-drawer)
# are roadmap, not here.
# Parameter variables carry `# spec:` comments; packaging/bundle.py extracts
# the UI spec from them. Run standalone with build123d installed, or use
# through the orcad tab.
from build123d import *
import math

GX = 4  # spec: int label=Grid X unit=u min=1 max=6 step=1
GY = 4  # spec: int label=Grid Y unit=u min=1 max=6 step=1
T = 5  # spec: number label=Thickness unit=mm min=4.6 max=8 step=0.2
SOCKETS = True  # spec: bool label=Bin sockets
REFINED = False  # spec: bool label=Refined holes
MAGNETS = True  # spec: bool label=Magnet holes (6x2)
SCREW = False  # spec: bool label=Screw holes (M3)
CRUSH = True  # spec: bool label=Crush ribs
CHAMFER = True  # spec: bool label=Hole chamfer
PRINTABLE = False  # spec: bool label=Supportless hole tops
CORNERS = False  # spec: bool label=Holes only at corners

# Slab footprint: cells tile at the 42mm pitch (BASEPLATE_DIMENSIONS, gridfinity-baseplate.scad:19).
W = GX * 42.0
D = GY * 42.0
result = extrude(Plane.XY * RectangleRounded(W, D, 2.0), amount=T)
for _ix in range(GX):
    for _iy in range(GY):
        _cx = (_ix - (GX - 1) / 2) * 42.0
        _cy = (_iy - (GY - 1) / 2) * 42.0
        if SOCKETS:
            # socket per cell: tapered pocket approximating baseplate_cutter
            # (_BASEPLATE_PROFILE [[0,0],[0.7,0.7],[0.7,2.5],[2.85,4.65]],
            # gridfinity-baseplate.scad:38-43; bottom opening ~36.3 wide)
            _sockD = T - 1.2
            _sock = loft(Sketch() + [Pos(_cx, _cy, T - _sockD) * (Plane.XY * RectangleRounded(36.3, 36.3, 1.15)), Pos(_cx, _cy, 0) * (Plane.XY.offset(T + 0.5) * RectangleRounded(40.5, 40.5, 2.5))], ruled=True)
            result -= _sock
# ---- holes open at the top surface (mirrored block_base_hole) ----
def _plate_hole_xy():
    if CORNERS:
        _hx = (W - 5.9) / 2 - 4.8
        _hy = (D - 5.9) / 2 - 4.8
        return [(-_hx, -_hy), (-_hx, _hy), (_hx, -_hy), (_hx, _hy)]
    _pos = []
    for _ix in range(GX):
        for _iy in range(GY):
            _cx = (_ix - (GX - 1) / 2) * 42.0
            _cy = (_iy - (GY - 1) / 2) * 42.0
            for _dx in (-13.0, 13.0):
                for _dy in (-13.0, 13.0):
                    _pos.append((_cx + _dx, _cy + _dy))
    return _pos
def _plate_printable(_inner, _outer):
    # stepped bridging ceiling just under the top surface (mirrored
    # make_hole_printable); local frame spans the top 0.84 of the hole
    _od = 2 * (_outer + 0.02)
    _id = 2 * (_inner + 0.02)
    _per = (_od - _id) / 2
    _solid = Pos(-(_od + 0.02) / 2, -(_od + 0.02) / 2, 1.68) * Box(_od + 0.02, _od + 0.02, 0.84, align=(Align.MIN, Align.MIN, Align.MIN))
    for _w1, _w2, _zz, _rt in ((_od, _od - _per, 1.66, False), (_od - _per, _od - 2 * _per, 1.86, True), (_od - 2 * _per, _od - 2 * _per, 2.06, False)):
        _a, _b = (_w2, _w1) if _rt else (_w1, _w2)
        _solid -= Pos(-_a / 2, -_b / 2, _zz) * Box(_a, _b, 0.24, align=(Align.MIN, Align.MIN, Align.MIN))
    return _solid
if REFINED or MAGNETS or SCREW:
    for (_hx, _hy) in _plate_hole_xy():
        if REFINED:
            # refined hole, single orientation (hole_pattern has no rotation)
            _ref = Pos(0, -2.93, -1.9) * Box(11, 5.86, 1.9, align=(Align.MIN, Align.MIN, Align.MIN))
            _ref += Pos(0, 0, -1.9) * Cylinder(2.93, 1.9, align=(Align.CENTER, Align.CENTER, Align.MIN))
            _ref += Pos(-6.93, -1.25, -2.5) * Box(4.4, 2.5, 2.5, align=(Align.MIN, Align.MIN, Align.MIN))
            _ref += Pos(-6.93, 0, -2.5) * Cylinder(1.25, 2.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
            result -= Pos(_hx, _hy, T) * _ref
        if MAGNETS:
            if CRUSH:
                _pts = []
                for _i in range(64):
                    _a = _i * 360.0 / 64
                    _r = 3.1 + 0.15 * math.sin(math.radians(_a * 8))
                    _pts.append((_r * math.sin(math.radians(_a)), _r * math.cos(math.radians(_a))))
                _pts.append(_pts[0])
                _mhole = extrude(Plane.XY * make_face(Polyline(*_pts)), amount=2.4)
            else:
                _mhole = Cylinder(3.25, 2.4, align=(Align.CENTER, Align.CENTER, Align.MIN))
            if PRINTABLE:
                _mhole -= _plate_printable(1.5 if SCREW else 1.0, 3.25)
            if CHAMFER:
                _mhole += Cone(1.65, 4.05, 2.4, align=(Align.CENTER, Align.CENTER, Align.MIN))
            result -= Pos(_hx, _hy, T - 2.4) * _mhole
        if SCREW:
            _shole = Pos(0, 0, -0.25) * Cylinder(1.5, T + 0.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
            if PRINTABLE:
                _shole -= Pos(0, 0, T - 2.4) * _plate_printable(0.5, 1.5)
            if CHAMFER:
                _shole += Pos(0, 0, T - 0.8) * Cone(1.5, 2.3, 0.8, align=(Align.CENTER, Align.CENTER, Align.MIN))
            result -= Pos(_hx, _hy, 0) * _shole
