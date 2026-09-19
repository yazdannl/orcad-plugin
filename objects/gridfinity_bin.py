# object: Gridfinity Bin
# blurb: Rebuilt-style bin: lofted foot, tapered lip, dividers, scoop, magnets.
# Gridfinity Bin for orcad — runnable build123d program (see header above).
from build123d import *

GX = 2  # spec: int label=Grid X unit=u min=1 max=6 step=1
GY = 2  # spec: int label=Grid Y unit=u min=1 max=6 step=1
HU = 6  # spec: int label=Height unit=u min=1 max=12 step=1
WALL = 1.2  # spec: number label=Wall unit=mm min=0.8 max=2.4 step=0.2
DX = 0  # spec: int label=Dividers X min=0 max=4 step=1
DY = 0  # spec: int label=Dividers Y min=0 max=4 step=1
MAGNETS = True  # spec: bool label=Magnet holes (6x2)
LIP = True  # spec: bool label=Stacking lip
SCOOP = False  # spec: bool label=Scoop notch (front)

# spec constants (src/core/standard.scad): pitch 42.0 (:16), gap 0.5 (:205),
# base 7.0 (:217), profile 4.75 (:211), top radius 3.75 (:190)
BASE_H = 7.0
PROF_H = 4.75
W = GX * 42.0 - 0.5  # grid_size_mm, base.scad:36-41
D = GY * 42.0 - 0.5
H = HU * 7.0  # fromGridfinityUnits, utility:24 (incl base, excl lip)
# body: rounded walls 4.75..H fused onto the feet below; open-top cavity
# from the infill floor at 7.0 (bin_render_infill, bin.scad:160)
_outer = Pos(0, 0, PROF_H) * extrude(Plane.XY * RectangleRounded(W, D, 3.75), amount=H - PROF_H)
_cw = W - 2 * WALL
_cd = D - 2 * WALL
_cavity = Pos(0, 0, BASE_H) * extrude(Plane.XY * RectangleRounded(_cw, _cd, max(0.5, 3.75 - WALL)), amount=H - BASE_H + 1)
result = _outer - _cavity
# stacking feet: BASE_PROFILE [[0,0],[0.8,0.8],[0.8,2.6],[2.95,4.75]]
# (standard.scad:175) -> (z, width, corner): bottom 35.6 = 41.5-2*2.95
# (base_bottom_dimensions, :236), mid 37.2 = 35.6+2*0.8, top 41.5;
# bottom corner 0.8 = BASE_BOTTOM_RADIUS (:229), top 3.75; mid transitions
# are sharp miters in spec, 0.8 keeps the ruled loft stable
_prof = [(0.0, 35.6, 0.8), (0.8, 37.2, 0.8), (2.6, 37.2, 0.8), (4.75, 41.5, 3.75)]
for _ix in range(GX):
    for _iy in range(GY):
        _cx = (_ix - (GX - 1) / 2) * 42.0  # cells centered on the bin
        _cy = (_iy - (GY - 1) / 2) * 42.0
        _secs = [Pos(_cx, _cy, 0) * (Plane.XY.offset(_z) * RectangleRounded(_w, _w, _r)) for _z, _w, _r in _prof]
        result += loft(Sketch() + _secs, ruled=True)
# magnet holes: r 3.25, depth 2.4 (standard.scad:28-29), at
# base_bottom/2 - 4.8 = 17.8-4.8 = 13.0 from cell center (:35, base.scad:272)
if MAGNETS:
    for _ix in range(GX):
        for _iy in range(GY):
            _cx = (_ix - (GX - 1) / 2) * 42.0
            _cy = (_iy - (GY - 1) / 2) * 42.0
            for _dx in (-13.0, 13.0):
                for _dy in (-13.0, 13.0):
                    result -= Pos(_cx + _dx, _cy + _dy, -0.5) * Cylinder(3.25, 2.9, align=(Align.CENTER, Align.CENTER, Align.MIN))
# Stacking lip: STACKING_LIP_LINE [[0,0],[0.7,0.7],[0.7,2.5],[2.6,4.4]]
# (standard.scad:124), nominal height 4.4 (:144, actual ~3.55 with 0.6
# fillet); outer face flush with the walls, inner funnel from W-5.2
# (2x2.6) at the 1.2 support up to W at the rim (wall.scad:25-46)
if LIP:
    _lo0 = W - WALL
    _lo1 = W + WALL + 0.6
    _li0 = _cw - 0.2
    _li1 = _cw + 2 * WALL + 0.4
    _lip_outer = loft(Sketch() + [Plane.XY.offset(H) * RectangleRounded(_lo0, _lo0, 3.0), Plane.XY.offset(H + 4.4) * RectangleRounded(_lo1, _lo1, 3.5)], ruled=True)
    _lip_inner = loft(Sketch() + [Plane.XY.offset(H - 0.5) * RectangleRounded(_li0, _li0, 2.5), Plane.XY.offset(H + 4.5) * RectangleRounded(_li1, _li1, 3.0)], ruled=True)
    result += _lip_outer - _lip_inner
# divider walls (compartment count DX+1 x DY+1; nominal d_div 1.2, standard.scad:10)
if DX or DY:
    # Divider walls top out below the lip support when lipped
    # (STACKING_LIP_SUPPORT_HEIGHT=1.2, standard.scad:118; else infill runs to H).
    _div0 = BASE_H
    _div1 = H - 1.2 if LIP else H
    if _div1 > _div0:  # 1U bins have no infill, so no dividers (bin.scad:256)
        if DX:
            for _i in range(1, DX + 1):
                _x = -_cw / 2 + _i * _cw / (DX + 1)
                result += Pos(_x, 0, _div0) * Box(WALL, _cd, _div1 - _div0, align=(Align.CENTER, Align.CENTER, Align.MIN))
        if DY:
            for _j in range(1, DY + 1):
                _y = -_cd / 2 + _j * _cd / (DY + 1)
                result += Pos(0, _y, _div0) * Box(_cw, WALL, _div1 - _div0, align=(Align.CENTER, Align.CENTER, Align.MIN))
# scoop notch in the front wall (simplified finger pull; .scad scoops the compartment instead, cutouts.scad:141)
if SCOOP:
    _nw = min(_cw * 0.6, _cw - 2 * WALL)
    _nh = (H - BASE_H) * 0.45 + 1
    _notch = Pos(-_nw / 2, D / 2 - WALL - 1, H + 1 - _nh) * Box(_nw, WALL + 2, _nh, align=(Align.MIN, Align.MIN, Align.MIN))
    result -= _notch
