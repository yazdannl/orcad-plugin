"""Tube predefined object for orcad.

Standalone module: exposes SPEC (parameter UI) and generate(c) which returns
a self-contained build123d program assigning `result`. No imports allowed
(single-file bundling constraint) — see packaging/bundle.py.
"""

SPEC = {
    "label": "Tube",
    "blurb": "Hollow cylinder.",
    "params": [
        {"key": "R_OUT", "label": "Outer radius", "unit": "mm", "ptype": "number", "min": 1, "max": 150, "step": 0.5, "default": 12},
        {"key": "R_IN", "label": "Inner radius", "unit": "mm", "ptype": "number", "min": 0.5, "max": 149, "step": 0.5, "default": 8},
        {"key": "H", "label": "Height", "unit": "mm", "ptype": "number", "min": 1, "max": 300, "step": 0.5, "default": 25},
    ],
}


def generate(c):
    return (
        "from build123d import *\n"
        f"result = Cylinder({c['R_OUT']}, {c['H']}) - Cylinder({c['R_IN']}, {c['H']} + 2)\n"
    )
