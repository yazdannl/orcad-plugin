"""orcad predefined objects — one module per object.

Each module exposes SPEC (parameter UI) and generate(c) (build123d program).
orcad.py bundles them into its single-file Hub artifact; run
`python3 packaging/bundle.py --check` to verify they are in sync.
"""

from . import box
from . import bracket
from . import cylinder
from . import gridfinity_baseplate
from . import gridfinity_bin
from . import tube

OBJECTS = {
    "box": box,
    "bracket": bracket,
    "cylinder": cylinder,
    "gridfinity_baseplate": gridfinity_baseplate,
    "gridfinity_bin": gridfinity_bin,
    "tube": tube,
}
