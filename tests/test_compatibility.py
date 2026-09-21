"""Tests for the machine-readable supported-version declarations."""
import json
from pathlib import Path

import pytest

from verify.compatibility import CompatibilityError, load_manifest, satisfies, validate_manifest


ROOT = Path(__file__).resolve().parents[1]


def test_version_constraints_handle_exact_and_open_ranges():
    assert satisfies("3.14.4", ">=3.12,<3.15")
    assert satisfies("0.12.0", "==0.12.0")
    assert satisfies("24.21.0", ">=20.19.0,<25")
    assert not satisfies("3.15.0", ">=3.12,<3.15")
    assert not satisfies("0.13.0", "==0.12.0")
    assert not satisfies("25.0.0", ">=20.19.0,<25")


def test_malformed_version_constraints_are_rejected():
    with pytest.raises(CompatibilityError):
        satisfies("3.14.4", "~=3.14")
    with pytest.raises(CompatibilityError):
        satisfies("not-a-version", ">=3")


def test_manifest_tested_versions_and_source_metadata_are_consistent():
    manifest = load_manifest()
    validate_manifest(manifest)
    assert manifest["host"]["orcaslicer"]["stable_2_4_2"]["supported"] is False
    assert manifest["matrix"][-1]["status"] == "optional"

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "compatibility.json" in readme
    assert "verify/compatibility.py --run" in readme
    assert "Supported versions" in changelog


def test_manifest_is_valid_json_object():
    parsed = json.loads((ROOT / "compatibility.json").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    assert parsed["schema"] == 1
