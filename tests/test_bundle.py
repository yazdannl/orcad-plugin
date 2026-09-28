"""Release bundling: deterministic embedded blobs and version agreement."""
import base64
import gzip
import importlib.util
import io
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("bundle", ROOT / "packaging" / "bundle.py")
bundle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bundle)


def test_backend_archive_is_deterministic_and_complete():
    first = bundle.backend_archive()
    assert first == bundle.backend_archive()
    names = set(zipfile.ZipFile(io.BytesIO(first)).namelist())
    assert {"openscad/__init__.py", "openscad/catalog.json", "openscad/objects/box.scad",
            "openscad/vendor/gridfinity-rebuilt-openscad-910e22d8/LICENSE",
            "openscad/vendor/gridfinity-rebuilt-openscad-910e22d8/src/core/bin.scad"} <= names
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)


def test_render_target_fills_both_regions_idempotently():
    source = ("x = 1\n# BEGIN EMBEDDED OPENSCAD BACKEND\n_EMBEDDED_BACKEND = \"\"\n# END EMBEDDED OPENSCAD BACKEND\n"
              "# BEGIN BUNDLED FRONTEND\n_EMBEDDED_FRONTEND = \"\"\n# END BUNDLED FRONTEND\n")
    once = bundle.render_target(source, "<html>page</html>")
    assert bundle.render_target(once, "<html>page</html>") == once
    blob = re.search(r'_EMBEDDED_FRONTEND = "([^"]+)"', once).group(1)
    assert gzip.decompress(base64.b64decode(blob)) == b"<html>page</html>"


def test_versions_agree():
    plugin = (ROOT / "orcad.py").read_text(encoding="utf-8")
    header = re.search(r'^# version = "([^"]+)"', plugin, re.M).group(1)
    constant = re.search(r'^PLUGIN_VERSION = "([^"]+)"', plugin, re.M).group(1)
    package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))["version"]
    assert header == constant == package
    assert '# dependencies = []' in plugin  # the plugin must install without pulling wheels
