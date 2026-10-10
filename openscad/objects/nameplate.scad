// orcad: nameplate generator. Multi-line text on a shaped plate, raised,
// engraved, cut through or inlaid for a two-colour print, with optional
// auto-sizing and screw, magnet, keyhole and keyring mounting holes.
// Text metrics use the experimental textmetrics() builtin; when it is not
// enabled (or unavailable) the layout falls back to rough advances.

/* [Text] */
text = "OrcaCAD";
font_style = 2;           // [0:Default, 1:Sans, 2:Sans bold, 3:Sans italic, 4:Serif, 5:Serif bold, 6:Mono, 7:Mono bold]
text_size = 12;
letter_spacing = 1;
line_spacing = 1.25;
text_align = 0;           // [0:Center, 1:Left, 2:Right]
text_style = 0;           // [0:Raised, 1:Engraved, 2:Cut through, 3:Inlay]
text_depth = 1.2;
fit_text = true;

/* [Plate] */
auto_size = false;
padding_x = 4;
padding_y = 3;
plate_shape = 1;          // [0:Rectangle, 1:Rounded, 2:Pill, 3:Oval, 4:Hexagon, 5:Chamfered]
plate_width = 90;
plate_height = 30;
plate_thickness = 4;
corner_radius = 4;
top_bevel = 1;
border = false;
border_width = 3;
border_height = 1;
inlay_padding = 2.5;

/* [Mounting] */
screw_holes = false;
hole_count = 2;           // [2:Two, 4:Four]
hole_diameter = 3.4;
hole_inset = 5;
countersink = false;
screw_head_diameter = 6.5;
magnet_holes = false;
magnet_count = 4;         // [2:Two, 4:Four]
magnet_diameter = 6.2;
magnet_depth = 2.4;
magnet_inset = 7;
keyhole = false;
keyhole_count = 1;        // [1:One, 2:Two]
keyhole_head_diameter = 8;
keyhole_slot_width = 4.5;
keyhole_depth = 2.4;
keyhole_length = 12;
keyring = false;
keyring_diameter = 5;
keyring_inset = 2;
keyring_side = 1;         // [0:Left, 1:Right]

// ---------------------------------------------------------------- text layout

function _font() =
    font_style == 1 ? "Liberation Sans:style=Regular" :
    font_style == 2 ? "Liberation Sans:style=Bold" :
    font_style == 3 ? "Liberation Sans:style=Italic" :
    font_style == 4 ? "Liberation Serif:style=Regular" :
    font_style == 5 ? "Liberation Serif:style=Bold" :
    font_style == 6 ? "Liberation Mono:style=Regular" :
    font_style == 7 ? "Liberation Mono:style=Bold" : "";

// OpenSCAD 2023.09 has no substr()/split(), so walk the string one character
// at a time. The text model is small enough for the repeated concatenation.
function _split(s, i = 0, line = "", acc = []) =
    i >= len(s) ? concat(acc, [line])
    : s[i] == "\n" ? _split(s, i + 1, "", concat(acc, [line]))
    : _split(s, i + 1, str(line, s[i]), acc);

_lines = _split(text);
_metrics = [for (line = _lines) textmetrics(line, size = 1, spacing = letter_spacing, font = _font())];

// textmetrics() is experimental: without --enable=textmetrics it is undef, so
// fall back to rough advances instead of failing the whole render.
function _adv(line, m) = is_undef(m) ? 0.58 * len(line) : m.advance[0];
function _asc(m) = is_undef(m) ? 0.72 : m.ascent;
function _dsc(m) = is_undef(m) ? -0.21 : m.descent;

_n = len(_lines);
_uw = [for (i = [0 : _n - 1]) _adv(_lines[i], _metrics[i])];
_ua = [for (m = _metrics) _asc(m)];
_ud = [for (m = _metrics) _dsc(m)];
_raw_w = max(_uw) * text_size;
_raw_h = (_n - 1) * text_size * line_spacing + (max(_ua) - min(_ud)) * text_size;

// Room the text needs around it: the inlay pocket and the border both eat into
// the plate, so the effective padding grows with them.
_pad_extra = text_style == 3 ? inlay_padding : 0;
_pad_border = border ? border_width + 1 : 0;
_pad_x = max(padding_x + _pad_extra, _pad_border);
_pad_y = max(padding_y + _pad_extra, _pad_border);
_plate_w = auto_size ? max(_raw_w + 2 * _pad_x, 12) : plate_width;
_plate_h = auto_size ? max(_raw_h + 2 * _pad_y, 8) : plate_height;
_avail_w = max(1, _plate_w - 2 * _pad_x);
_avail_h = max(1, _plate_h - 2 * _pad_y);
_scale = fit_text ? min(1, _avail_w / max(_raw_w, 0.01), _avail_h / max(_raw_h, 0.01)) : 1;
_size = text_size * _scale;
_lh = _size * line_spacing;
_advs = [for (u = _uw) u * _size];
_ascs = [for (u = _ua) u * _size];
_dscs = [for (u = _ud) u * _size];
_bw = max(_advs);
_bh = (_n - 1) * _lh + max(_ascs) - min(_dscs);
_baseline = _bh / 2 - max(_ascs);
_align_x = text_align == 1 ? -_bw / 2 : text_align == 2 ? _bw / 2 : 0;
_halign = text_align == 1 ? "left" : text_align == 2 ? "right" : "center";

module text_2d() {
    for (i = [0 : _n - 1])
        translate([_align_x, _baseline - i * _lh])
            text(_lines[i], size = _size, font = _font(), spacing = letter_spacing,
                 halign = _halign, valign = "baseline");
}

// -------------------------------------------------------------------- plate

_corner = max(0, min(corner_radius, _plate_w / 2 - 0.01, _plate_h / 2 - 0.01));
_bevel = max(0, min(top_bevel, plate_thickness - 0.2));

module raw_outline() {
    pw = max(_plate_w, _plate_h);
    if (plate_shape == 0) {
        square([_plate_w, _plate_h], center = true);
    } else if (plate_shape == 1) {
        offset(r = _corner) square([_plate_w - 2 * _corner, _plate_h - 2 * _corner], center = true);
    } else if (plate_shape == 2) {
        hull() {
            translate([-(pw - _plate_h) / 2, 0]) circle(d = _plate_h, $fn = 128);
            translate([(pw - _plate_h) / 2, 0]) circle(d = _plate_h, $fn = 128);
        }
    } else if (plate_shape == 3) {
        scale([_plate_w / 2, _plate_h / 2]) circle(r = 1, $fn = 256);
    } else if (plate_shape == 4) {
        polygon([[_plate_w / 2, 0], [_plate_w / 4, _plate_h / 2], [-_plate_w / 4, _plate_h / 2],
                 [-_plate_w / 2, 0], [-_plate_w / 4, -_plate_h / 2], [_plate_w / 4, -_plate_h / 2]]);
    } else {
        c = _corner;
        polygon([[_plate_w / 2 - c, _plate_h / 2], [_plate_w / 2, _plate_h / 2 - c],
                 [_plate_w / 2, -_plate_h / 2 + c], [_plate_w / 2 - c, -_plate_h / 2],
                 [-_plate_w / 2 + c, -_plate_h / 2], [-_plate_w / 2, -_plate_h / 2 + c],
                 [-_plate_w / 2, _plate_h / 2 - c], [-_plate_w / 2 + c, _plate_h / 2]]);
    }
}

// shrink > 0 insets the outline, for the top bevel and the border frame.
module outline(shrink = 0) {
    if (shrink > 0) offset(delta = -shrink) raw_outline(); else raw_outline();
}

module plate() {
    union() {
        linear_extrude(plate_thickness - _bevel) outline();
        if (_bevel > 0) hull() {
            translate([0, 0, plate_thickness - _bevel]) linear_extrude(0.01) outline();
            translate([0, 0, plate_thickness - 0.01]) linear_extrude(0.01) outline(shrink = _bevel);
        }
    }
}

module border_rim() {
    if (border) {
        bw = min(border_width, min(_plate_w, _plate_h) / 2 - 0.2);
        translate([0, 0, plate_thickness]) linear_extrude(border_height)
            difference() {
                outline(shrink = _bevel);
                outline(shrink = _bevel + bw);
            }
    }
}

// --------------------------------------------------------------------- text

_engrave = max(0.2, min(text_depth, plate_thickness - 0.4));

module raised_text() {
    translate([0, 0, plate_thickness]) linear_extrude(max(0.2, text_depth)) text_2d();
}

module engraved_text() {
    translate([0, 0, plate_thickness - _engrave]) linear_extrude(_engrave + 0.2) text_2d();
}

module cut_text() {
    translate([0, 0, -0.5]) linear_extrude(plate_thickness + 1) text_2d();
}

// A recessed pocket whose letters fill it back up to the top: swap filament at
// the pocket floor and the text prints in a second colour, flush with the plate.
module inlay_pocket() {
    pw = min(_bw + 2 * inlay_padding, _plate_w - 2);
    ph = min(_bh + 2 * inlay_padding, _plate_h - 2);
    translate([0, 0, plate_thickness - _engrave]) linear_extrude(_engrave + 0.2)
        difference() {
            offset(r = 1.2) square([max(1, pw - 2.4), max(1, ph - 2.4)], center = true);
            text_2d();
        }
}

module inlay_text() {
    translate([0, 0, plate_thickness - _engrave]) linear_extrude(_engrave) text_2d();
}

// ------------------------------------------------------------------ mounting

function _screw_positions() =
    let (ix = max(0, min(hole_inset, _plate_w / 2 - 0.5)),
         iy = max(0, min(hole_inset, _plate_h / 2 - 0.5)))
        hole_count == 4
            ? [[-ix, -iy], [ix, -iy], [-ix, iy], [ix, iy]]
            : [[-ix, 0], [ix, 0]];

function _magnet_positions() =
    let (ix = max(0, min(magnet_inset, _plate_w / 2 - 0.5)),
         iy = max(0, min(magnet_inset, _plate_h / 2 - 0.5)))
        magnet_count == 4
            ? [[-ix, -iy], [ix, -iy], [-ix, iy], [ix, iy]]
            : [[-ix, 0], [ix, 0]];

function _keyhole_positions() =
    keyhole_count == 2 ? [[-_plate_w / 4, 0], [_plate_w / 4, 0]] : [[0, 0]];

module screws() {
    if (screw_holes) {
        for (p = _screw_positions())
            translate([p[0], p[1], -0.5]) cylinder(d = hole_diameter, h = plate_thickness + 1, $fn = 64);
        if (countersink)
            for (p = _screw_positions()) {
                depth = max(0.2, (screw_head_diameter - hole_diameter) / 2);
                translate([p[0], p[1], plate_thickness - depth])
                    cylinder(h = depth + 0.2, d1 = hole_diameter, d2 = screw_head_diameter, $fn = 64);
            }
    }
}

module magnets() {
    if (magnet_holes) {
        depth = max(0.2, min(magnet_depth, plate_thickness - 0.4));
        for (p = _magnet_positions())
            translate([p[0], p[1], -0.01]) cylinder(d = magnet_diameter, h = depth + 0.01, $fn = 64);
    }
}

// Keyhole slots cut into the back: the head enters through the round pocket and
// the plate drops so the shank sits in the narrow slot above it.
module keyholes() {
    if (keyhole) {
        depth = max(0.2, min(keyhole_depth, plate_thickness - 0.4));
        for (p = _keyhole_positions())
            translate([p[0], p[1], 0]) linear_extrude(depth)
                union() {
                    translate([0, -keyhole_length / 2]) circle(d = keyhole_head_diameter, $fn = 64);
                    translate([-keyhole_slot_width / 2, -keyhole_length / 2])
                        square([keyhole_slot_width, keyhole_length]);
                }
    }
}

module keyring_hole() {
    if (keyring) {
        x = max(0, _plate_w / 2 - keyring_inset - keyring_diameter / 2);
        translate([keyring_side == 0 ? -x : x, 0, -0.5])
            cylinder(d = keyring_diameter, h = plate_thickness + 1, $fn = 64);
    }
}

// -------------------------------------------------------------------- output

difference() {
    union() {
        plate();
        border_rim();
        if (text_style == 0) raised_text();
        if (text_style == 3) inlay_text();
    }
    if (text_style == 1) engraved_text();
    if (text_style == 2) cut_text();
    if (text_style == 3) inlay_pocket();
    screws();
    magnets();
    keyholes();
    keyring_hole();
}
