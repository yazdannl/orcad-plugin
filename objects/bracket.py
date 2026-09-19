"""Bracket-plate predefined object for orcad.

Standalone module: exposes SPEC (parameter UI) and generate(c) which returns
a self-contained build123d program assigning `result`. No imports allowed
(single-file bundling constraint) — see packaging/bundle.py.
"""

SPEC = {
    "label": "Bracket plate",
    "blurb": "Flat plate with two holes.",
    "params": [
        {"key": "L", "label": "Length", "unit": "mm", "ptype": "number", "min": 10, "max": 300, "step": 0.5, "default": 60},
        {"key": "W", "label": "Width", "unit": "mm", "ptype": "number", "min": 10, "max": 200, "step": 0.5, "default": 30},
        {"key": "T", "label": "Thickness", "unit": "mm", "ptype": "number", "min": 1, "max": 50, "step": 0.5, "default": 5},
        {"key": "D", "label": "Hole dia", "unit": "mm", "ptype": "number", "min": 1, "max": 50, "step": 0.5, "default": 5},
    ],
}


def generate(c):
    return (
        "from build123d import *\n"
        f"L, W, T, D = {c['L']}, {c['W']}, {c['T']}, {c['D']}\n"
        "plate = Box(L, W, T)\n"
        "hole = Cylinder(D / 2, T + 2)\n"
        "h1 = Pos(-L / 4, 0, -1) * hole\n"
        "h2 = Pos(L / 4, 0, -1) * hole\n"
        "result = plate - h1 - h2\n"
    )
