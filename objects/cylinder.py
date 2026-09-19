"""Cylinder predefined object for orcad.

Standalone module: exposes SPEC (parameter UI) and generate(c) which returns
a self-contained build123d program assigning `result`. No imports allowed
(single-file bundling constraint) — see packaging/bundle.py.
"""

SPEC = {
    "label": "Cylinder",
    "blurb": "Round post or puck.",
    "params": [
        {"key": "R", "label": "Radius", "unit": "mm", "ptype": "number", "min": 0.5, "max": 150, "step": 0.5, "default": 10},
        {"key": "H", "label": "Height", "unit": "mm", "ptype": "number", "min": 1, "max": 300, "step": 0.5, "default": 20},
    ],
}


def generate(c):
    return f"from build123d import *\nresult = Cylinder({c['R']}, {c['H']})\n"
