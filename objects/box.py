"""Box predefined object for orcad.

Standalone module: exposes SPEC (parameter UI) and generate(c) which returns
a self-contained build123d program assigning `result`. No imports allowed
(single-file bundling constraint) — see packaging/bundle.py.
"""

SPEC = {
    "label": "Box",
    "blurb": "Simple centered block.",
    "params": [
        {"key": "L", "label": "Length", "unit": "mm", "ptype": "number", "min": 1, "max": 300, "step": 0.5, "default": 20},
        {"key": "W", "label": "Width", "unit": "mm", "ptype": "number", "min": 1, "max": 300, "step": 0.5, "default": 20},
        {"key": "H", "label": "Height", "unit": "mm", "ptype": "number", "min": 1, "max": 300, "step": 0.5, "default": 20},
    ],
}


def generate(c):
    return f"from build123d import *\nresult = Box({c['L']}, {c['W']}, {c['H']})\n"
