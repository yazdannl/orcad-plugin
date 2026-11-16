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


def test_the_payload_can_be_read_without_unpacking_it():
    archive = zipfile.ZipFile(io.BytesIO(bundle.backend_archive()))
    assert "PAYLOAD.md" in archive.namelist()
    manifest = archive.read("PAYLOAD.md").decode("utf-8")
    digest = __import__("hashlib").sha256(archive.read("ai/agent.py")).hexdigest()
    assert f"| `ai/agent.py` | {len(archive.read('ai/agent.py'))} | `{digest}` |" in manifest
    assert "native/mesh.c" in archive.namelist(), "the compiled mesh path ships with its source"


def test_archive_records_a_fixed_creating_system():
    # ZipInfo defaults to the build host, so a Windows build would embed a
    # different blob than the committed one and every CI check would fail.
    infos = zipfile.ZipFile(io.BytesIO(bundle.backend_archive())).infolist()
    assert infos and {info.create_system for info in infos} == {3}


def test_render_target_fills_every_region_idempotently():
    source = ("x = 1\n# BEGIN EMBEDDED OPENSCAD BACKEND\n_EMBEDDED_BACKEND = \"\"\n# END EMBEDDED OPENSCAD BACKEND\n"
              "# BEGIN BUNDLED FRONTEND\n_EMBEDDED_FRONTEND = \"\"\n# END BUNDLED FRONTEND\n"
              "# BEGIN EMBEDDED TAB ICON\n_EMBEDDED_ICON = \"\"\n# END EMBEDDED TAB ICON\n")
    once = bundle.render_target(source, "<html>page</html>")
    assert bundle.render_target(once, "<html>page</html>") == once
    blob = re.search(r'_EMBEDDED_FRONTEND = "([^"]+)"', once).group(1)
    assert gzip.decompress(base64.b64decode(blob)) == b"<html>page</html>"
    icon = re.search(r'_EMBEDDED_ICON = "([^"]+)"', once).group(1)
    assert gzip.decompress(base64.b64decode(icon)) == bundle.ICON.read_bytes()


def test_archive_carries_the_previews_the_viewer_and_the_native_fast_path():
    names = set(zipfile.ZipFile(io.BytesIO(bundle.backend_archive())).namelist())
    previews = {name for name in names if name.startswith("thumbs/")}
    assert previews == {f"thumbs/{path.name}" for path in bundle.THUMBS.glob("*.png")}
    assert len(previews) > 300 and "thumbs/box.png" in previews
    assert "viewport.js" in names, "a single-file install has to ship the viewer chunk"
    shipped = {name for name in names if name.startswith("openscad/native/dist/")}
    assert len(shipped) == 5, f"every platform's library ships in the archive: {sorted(shipped)}"


def test_the_page_stays_far_below_the_webview_limit():
    # OrcaSlicer hands the whole page to the webview in one call (WebView2
    # NavigateToString), which fails above 2 MB. Keep a stricter 1 MB budget;
    # the editor can make the page larger than the separate viewer chunk.
    assert bundle.DIST.is_file(), "run `npm run build` in frontend/"
    page = bundle.DIST.stat().st_size
    assert page < bundle.MAX_PAGE_BYTES, f"the page grew to {page // 1024} KB"
    assert bundle.VIEWPORT.is_file(), "the 3D viewer is delivered as a separate chunk"
    assert not (bundle.DIST.parent / "thumbs").exists(), "previews are served on demand, not bundled into the page"


def test_versions_agree():
    plugin = (ROOT / "orcad.py").read_text(encoding="utf-8")
    header = re.search(r'^# version = "([^"]+)"', plugin, re.M).group(1)
    constant = re.search(r'^PLUGIN_VERSION = "([^"]+)"', plugin, re.M).group(1)
    package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))["version"]
    backend = re.search(r'^BACKEND_VERSION = "([^"]+)"',
                        (ROOT / "openscad" / "__init__.py").read_text(encoding="utf-8"), re.M).group(1)
    # A folder install compares the backend's release with the plugin's, so a
    # mixed install falls back to the embedded copy instead of hanging.
    assert header == constant == package == backend
    assert '# dependencies = []' in plugin  # the plugin must install without pulling wheels
