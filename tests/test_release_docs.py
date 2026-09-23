"""Release metadata, licensing, and status wording checks."""
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_VERSION = "0.7.0"


def _json(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_authoritative_release_version_is_synchronized():
    manifest = _json("compatibility.json")
    assert manifest["plugin"]["version"] == RELEASE_VERSION
    assert manifest["plugin"]["version_authority"] == "compatibility.json plugin.version"

    plugin = (ROOT / "orcad.py").read_text(encoding="utf-8")
    package = _json("frontend/package.json")
    lock = _json("frontend/package-lock.json")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert f'PLUGIN_VERSION = "{RELEASE_VERSION}"' in plugin
    assert re.search(r'^# version = "' + re.escape(RELEASE_VERSION) + r'"$', plugin, re.MULTILINE)
    assert package["version"] == RELEASE_VERSION
    assert lock["version"] == RELEASE_VERSION
    assert lock["packages"][""]["version"] == RELEASE_VERSION
    assert f"authoritative plugin release version is **{RELEASE_VERSION}**" in readme
    assert f"## {RELEASE_VERSION} — Supported release and parity notes" in changelog
    assert "## Unreleased" not in changelog


def test_project_license_and_attribution_boundaries_are_explicit():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    third_party = (ROOT / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
    package = _json("frontend/package.json")

    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in license_text
    assert "Version 3, 19 November 2007" in license_text
    assert package["license"] == "AGPL-3.0-only"
    assert "AGPL-3.0-only" in notice
    assert "kennetek/gridfinity-rebuilt-openscad" in notice
    assert "MUST be verified" in notice
    assert "React" in third_party and "Three.js" in third_party
    assert "The MIT License" in third_party
    assert "not relicensed under" in third_party
    assert "AGPL-3.0-only" in third_party


def test_feature_status_and_installation_docs_match_supported_behavior():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for label in ("**Verified**", "**Approximate**", "**Experimental / host-dependent**", "**Unsupported / roadmap**"):
        assert label in readme
    for phrase in (
        "Generate + export",
        "Send to plate",
        "Bridge ready",
        "CAD/model ready",
        "bundled `uv`",
        "python_*.log",
        "trusted",
        "drag the",
        "npm test",
        "verify/compatibility.py --run",
    ):
        assert phrase in readme
    assert "complete port" not in readme.lower()
    assert "OS = all" not in readme
    assert "License recommendation" not in readme
    assert "upstream license" in readme.lower()
