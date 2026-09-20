# object: Gridfinity Bin
# blurb: Full Rebuilt port: compartments, tabs, scoop, holes, lip, height modes.
# Faithful port of kennetek/gridfinity-rebuilt-openscad
# (gridfinity-rebuilt-bins.scad). Construction mirrors the original CSG tree:
# tapered feet + bridge + base holes, wall ring, infill solid, per-compartment
# rounded cutters (minus scoop/tab solids) or cylinders, stacking lip ring.
# Parameter variables carry `# spec:` comments; packaging/bundle.py extracts
# the UI spec from them. Run standalone with build123d installed, or use
# through the orcad tab.
from build123d import *
import math

GX = 2  # spec: int label=Grid X unit=u min=1 max=6 step=1
GY = 2  # spec: int label=Grid Y unit=u min=1 max=6 step=1
HU = 6  # spec: int label=Height value min=0 max=200 step=1
HMODE = 0  # spec: int label=Height mode 0U 1in 2ex 3exlip min=0 max=3 step=1
ZS = False  # spec: bool label=Snap height to 7mm
FILL = 0  # spec: number label=Solid fill mm (0=auto) unit=mm min=0 max=200 step=1
WALL = 0.95  # spec: number label=Outer wall unit=mm min=0.95 max=2.4 step=0.05
DX = 1  # spec: int label=Divisions X (0=solid) min=0 max=6 step=1
DY = 1  # spec: int label=Divisions Y (0=solid) min=0 max=6 step=1
DEPTH = 0  # spec: number label=Compartment depth mm (0=full) unit=mm min=0 max=200 step=1
SCOOPW = 1.0  # spec: number label=Scoop amount min=0 max=1 step=0.1
TABSTYLE = 1  # spec: int label=Tab 0Full 1Auto 2Left 3Center 4Right 5None min=0 max=5 step=1
TABPLACE = 0  # spec: int label=Tabs only top-left min=0 max=1 step=1
CYL = False  # spec: bool label=Cylindrical compartments
CD = 10  # spec: number label=Cylinder dia unit=mm min=1 max=60 step=0.5
CCHAM = 0.5  # spec: number label=Cylinder top chamfer unit=mm min=0 max=5 step=0.1
REFINED = True  # spec: bool label=Refined holes
MAGNETS = False  # spec: bool label=Magnet holes (6x2)
SCREW = False  # spec: bool label=Screw holes (M3)
CRUSH = True  # spec: bool label=Crush ribs
CHAMFER = True  # spec: bool label=Hole chamfer
PRINTABLE = True  # spec: bool label=Supportless hole tops
CORNERS = False  # spec: bool label=Holes only at corners
THUMB = False  # spec: bool label=Thumbscrew holes
LIP = True  # spec: bool label=Stacking lip

# ---- height (gridfinity-rebuilt-utility.scad: height() + z_snap) ----
_Hraw = HU * 7.0 if HMODE == 0 else (HU + 7.0 if HMODE == 1 else (HU if HMODE == 2 else HU - 4.4))
if ZS:
    _Hraw = _Hraw if _Hraw % 7 == 0 else _Hraw + 7 - _Hraw % 7
H = max(_Hraw, 7.0)
assert H >= 7.0, "height below 7mm base"
assert not LIP or FILL <= 0 or FILL <= H - 1.2, "fill too tall for lipped bin"
W = GX * 42.0 - 0.5
D = GY * 42.0 - 0.5
_EW = WALL
_inner_w = W - 2 * _EW
_inner_d = D - 2 * _EW
_inner_r = max(0.01, 3.75 - _EW)
assert _inner_w > 0 and _inner_d > 0, "wall leaves no interior"
_lip_sup = 1.2 if LIP else 0.0
_fill = FILL if FILL > 0 else H - 7.0 - _lip_sup
_infill_top = 7.0 + _fill
# ---- tapered stacking feet, one per cell (lofted spec profile) ----
_feet = None
_prof = [(0.0, 35.6, 0.8), (0.8, 37.2, 0.8), (2.6, 37.2, 0.8), (4.75, 41.5, 3.75)]
for _ix in range(GX):
    for _iy in range(GY):
        _cx = (_ix - (GX - 1) / 2) * 42.0
        _cy = (_iy - (GY - 1) / 2) * 42.0
        _secs = [Pos(_cx, _cy, 0) * (Plane.XY.offset(_z) * RectangleRounded(_w, _w, _r)) for _z, _w, _r in _prof]
        _foot = loft(Sketch() + _secs, ruled=True)
        _feet = _foot if _feet is None else _feet + _foot
result = _feet
# ---- bridge slab tying the feet together ----
result += Pos(0, 0, 4.75) * extrude(Plane.XY * RectangleRounded(W, D, 3.75), amount=2.25)
# ---- base holes (magnet/screw/refined options per cell or outer corners) ----
def _hole_positions():
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
def _printable_steps(_inner, _outer, _h):
    # stepped bridging ceiling, literal port of make_hole_printable (3 layers)
    _od = 2 * (_outer + 0.02)
    _id = 2 * (_inner + 0.02)
    _per = (_od - _id) / 2
    _adj = _h - 0.6
    _solid = Pos(-(_od + 0.02) / 2, -(_od + 0.02) / 2, _adj) * Box(_od + 0.02, _od + 0.02, 0.72, align=(Align.MIN, Align.MIN, Align.MIN))
    for _k, _w1, _w2, _zz, _rt in ((1, _od, _od - _per, _adj - 0.02, False), (2, _od - _per, _od - 2 * _per, _adj + 0.18, True), (3, _od - 2 * _per, _od - 2 * _per, _adj + 0.38, False)):
        _a, _b = (_w2, _w1) if _rt else (_w1, _w2)
        _solid -= Pos(-_a / 2, -_b / 2, _zz) * Box(_a, _b, 0.24, align=(Align.MIN, Align.MIN, Align.MIN))
    return _solid
if REFINED or MAGNETS or SCREW or THUMB:
    _positions = _hole_positions()
    if REFINED:
        # refined hole: side-entry slot + poke hole, rotated per quadrant
        _ref = Pos(0, -2.93, 0.4) * Box(11, 5.86, 1.9, align=(Align.MIN, Align.MIN, Align.MIN))
        _ref += Pos(0, 0, 0.4) * Cylinder(2.93, 1.9, align=(Align.CENTER, Align.CENTER, Align.MIN))
        _ref += Pos(-6.93, -1.25, -0.2) * Box(4.4, 2.5, 2.5, align=(Align.MIN, Align.MIN, Align.MIN))
        _ref += Pos(-6.93, 0, -0.2) * Cylinder(1.25, 2.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
        for (_qx, _qy, _rot) in ((1, 1, 0), (-1, 1, 90), (-1, -1, 180), (1, -1, 270)):
            for (_hx, _hy) in _positions:
                if (_hx > 0) == (_qx > 0) and (_hy > 0) == (_qy > 0):
                    result -= Pos(_hx, _hy, 0) * Rot(0, 0, _rot) * _ref
    if MAGNETS:
        _mdepth = 2.4 + (0.6 if PRINTABLE else 0.0)
        if CRUSH:
            _pts = []
            for _i in range(64):
                _a = _i * 360.0 / 64
                _r = 3.1 + 0.15 * math.sin(math.radians(_a * 8))
                _pts.append((_r * math.sin(math.radians(_a)), _r * math.cos(math.radians(_a))))
            _pts.append(_pts[0])
            _mhole = extrude(Plane.XY * make_face(Polyline(*_pts)), amount=_mdepth)
        else:
            _mhole = Cylinder(3.25, _mdepth, align=(Align.CENTER, Align.CENTER, Align.MIN))
        if PRINTABLE:
            _mhole -= _printable_steps(1.5 if SCREW else 1.0, 3.25, _mdepth)
        if CHAMFER:
            _mhole += Cone(4.05, max(0.05, 4.05 - 2.4), 2.4, align=(Align.CENTER, Align.CENTER, Align.MIN))
        for (_hx, _hy) in _positions:
            result -= Pos(_hx, _hy, 0) * _mhole
    if SCREW:
        _shole = Cylinder(1.5, 7.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
        if PRINTABLE:
            _shole -= _printable_steps(0.5, 1.5, 7.0)
        if CHAMFER:
            _shole += Cone(2.3, 0, 2.3, align=(Align.CENTER, Align.CENTER, Align.MIN))
        for (_hx, _hy) in _positions:
            result -= Pos(_hx, _hy, 0) * _shole
    if THUMB:
        for (_tx, _ty) in ([((_ix - (GX - 1) / 2) * 42.0, (_iy - (GY - 1) / 2) * 42.0) for _ix in range(GX) for _iy in range(GY)] if not CORNERS else [(_hx / 2, _hy / 2) for _hx in (-(W - 41.5) / 2, (W - 41.5) / 2) for _hy in (-(D - 41.5) / 2, (D - 41.5) / 2)]):
            result -= Pos(_tx, _ty, 0) * Cylinder(7.8, 4.75, align=(Align.CENTER, Align.CENTER, Align.MIN))
# ---- walls: thin ring + infill solid ----
if H > 7.0:
    _wall = extrude(Plane.XY * RectangleRounded(W, D, 3.75), amount=H - 7.0) - Pos(0, 0, -0.5) * extrude(Plane.XY * RectangleRounded(_inner_w, _inner_d, _inner_r), amount=H - 7.0 + 1)
    result += Pos(0, 0, 7.0) * _wall
if _fill > 0:
    # Keep infill inside the requested outer wall; otherwise it would hide
    # thicker walls when the compartment cutters are applied.
    result += Pos(0, 0, 7.0) * extrude(Plane.XY * RectangleRounded(_inner_w, _inner_d, _inner_r), amount=_fill)
# ---- compartments: per-division rounded cutters (element minus 0.6 total),
# minus scoop/tab solids; cylinders replace cutters when CYL ----
if DX > 0 and DY > 0 and _fill > 0:
    # Compartments tile the requested interior; the 0.6mm subtraction leaves
    # the fixed divider/edge web inside the wall.
    _rx = _inner_w / DX
    _ry = _inner_d / DY
    _ztop = _infill_top + 0.02
    _dep = DEPTH if DEPTH > 0 else _fill
    for _ix in range(DX):
        for _iy in range(DY):
            _cx = -_inner_w / 2 + (_ix + 0.5) * _rx
            _cy = -_inner_d / 2 + (_iy + 0.5) * _ry
            if CYL:
                _ccut = Cylinder(CD / 2, _dep + 0.02, align=(Align.CENTER, Align.CENTER, Align.MIN))
                if CCHAM > 0:
                    _ccut += Pos(0, 0, _dep + 0.02 - CCHAM) * Cone(CD / 2, CD / 2 + CCHAM, CCHAM, align=(Align.CENTER, Align.CENTER, Align.MIN))
                result -= Pos(_cx, _cy, _ztop - _dep) * _ccut
                continue
            _cw = _rx - 0.6
            _cd = _ry - 0.6
            _ch = _dep + 0.02
            _cr = min(2.8, _cw / 2 - 0.01, _cd / 2 - 0.01, _ch / 2 - 0.01)
            _cut = Box(_cw, _cd, _ch)
            if _cr > 0.5:
                _cut = fillet(_cut.edges(), _cr)
            _cut = Pos(0, 0, -_ch / 2) * _cut
            if SCOOPW > 0:
                # finger ramp at the -y wall: box minus x-axis cylinder
                _s = SCOOPW * _dep / 2
                if _s > 0.1:
                    _scoop = Pos(-_cw / 2, -_cd / 2, -_ch) * Box(_cw, _s, _s, align=(Align.MIN, Align.MIN, Align.MIN)) - Pos(0, -_cd / 2 + _s, -_ch + _s) * Rot(0, 90, 0) * Cylinder(_s, _cw + 2)
                    _cut -= _scoop
            # NOTE: the original only documents tab auto-disable below 3U but does
            # not enforce it; tabs render at any height exactly like here.
            _tabbed = TABSTYLE != 5 and (not TABPLACE or (_ix == 0 and _iy == DY - 1))
            if _tabbed:
                # label wedge on the +y wall: exact TAB_POLYGON profile
                _tw = max(_cw, _cd, _dep) if TABSTYLE == 0 else 42.0
                if TABSTYLE == 2:
                    _tx0 = -_cw / 2
                elif TABSTYLE == 4:
                    _tx0 = _cw / 2 - _tw
                elif TABSTYLE == 1:
                    _tx0 = -_cw / 2 if _ix == 0 else (_cw / 2 - _tw if _ix == DX - 1 else -_tw / 2)
                else:
                    _tx0 = -_tw / 2
                _th = 0.7265 * 15.85 + 1.2
                _tpts = [(_cd / 2, -_th), (_cd / 2, 0), (_cd / 2 - 15.85, 0), (_cd / 2 - 15.85, -1.2), (_cd / 2, -_th)]
                _tab = extrude(Plane.YZ * make_face(Polyline(*_tpts)), amount=_tw)
                _cut -= Pos(_tx0, 0, 0) * _tab
            result -= Pos(_cx, _cy, _ztop) * _cut
# ---- stacking lip: measured ring profile (outer flush with the walls,
# funnel void, rounded tip; total height H + 3.55) ----
if LIP:
    _lip_prof = [(-1.2, 0.0, 3.75, 2.6, 2.5), (0.0, 0.0, 3.75, 2.6, 2.5), (2.4, 0.04, 3.75, 1.9, 2.5), (3.0, 0.05, 3.75, 1.4, 2.5), (3.3, 0.13, 3.5, 1.1, 2.5), (3.5, 0.37, 3.2, 0.84, 2.0)]
    _ob0 = max(H - 3.0, 6.5)
    _ib0 = max(H - 3.5, 6.5)
    _lo = [Plane.XY.offset(_ob0) * RectangleRounded(W, D, 3.75)] + [Plane.XY.offset(H + _dz) * RectangleRounded(W - 2 * _oi, D - 2 * _oi, _or) for _dz, _oi, _or, _vi, _vr in _lip_prof]
    _li = [Plane.XY.offset(_ib0) * RectangleRounded(W - 2 * 1.25, D - 2 * 1.25, 2.5)] + [Plane.XY.offset(H + _dz) * RectangleRounded(W - 2 * _vi, D - 2 * _vi, _vr) for _dz, _oi, _or, _vi, _vr in _lip_prof]
    _lip_outer = loft(Sketch() + _lo + [Plane.XY.offset(H + 3.55) * RectangleRounded(W - 1.1, D - 1.1, 3.0)], ruled=True)
    _lip_inner = loft(Sketch() + _li, ruled=True)
    result += _lip_outer - _lip_inner
