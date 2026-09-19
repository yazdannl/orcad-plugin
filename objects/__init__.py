"""orcad predefined objects — runnable build123d programs, one file each.

Parameter variables in each file carry `# spec:` comments; the UI spec is
extracted by packaging/bundle.py (never import these modules — they execute
CAD geometry on import and need build123d installed).
"""

OBJECTS = ["box", "bracket", "cylinder", "gridfinity_baseplate", "gridfinity_bin", "tube"]
