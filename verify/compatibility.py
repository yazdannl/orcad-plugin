#!/usr/bin/env python3
"""Check the declared local compatibility matrix.

The default check validates tool versions and, when present, the optional CAD
virtualenv.  ``--run`` executes the four matrix stages in their documented
order.  This is a deterministic local check, not a claim that a CI provider
exists.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "compatibility.json"
VERSION_RE = re.compile(r"^(?:v)?(\d+(?:\.\d+){0,})")


class CompatibilityError(ValueError):
    """A declared compatibility requirement is not satisfied."""


def parse_version(value: str) -> tuple[int, ...]:
    """Parse the numeric prefix of a tool version for constraint checks."""
    match = VERSION_RE.match(str(value).strip())
    if not match:
        raise CompatibilityError(f"invalid version: {value!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def _normalized(version: tuple[int, ...], length: int) -> tuple[int, ...]:
    return version + (0,) * (length - len(version))


def _compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    size = max(len(left), len(right))
    a, b = _normalized(left, size), _normalized(right, size)
    return (a > b) - (a < b)


def satisfies(version: str, constraint: str) -> bool:
    """Return whether a numeric version satisfies comma-separated comparisons."""
    actual = parse_version(version)
    parts = [part.strip() for part in constraint.split(",") if part.strip()]
    if not parts:
        raise CompatibilityError(f"empty version constraint: {constraint!r}")
    for part in parts:
        match = re.fullmatch(r"(==|!=|>=|<=|>|<)\s*(\d+(?:\.\d+){0,})", part)
        if not match:
            raise CompatibilityError(f"invalid version constraint: {constraint!r}")
        expected = parse_version(match.group(2))
        comparison = _compare(actual, expected)
        operator = match.group(1)
        if not {
            "==": comparison == 0,
            "!=": comparison != 0,
            ">=": comparison >= 0,
            "<=": comparison <= 0,
            ">": comparison > 0,
            "<": comparison < 0,
        }[operator]:
            return False
    return True


def load_manifest() -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1:
        raise CompatibilityError("compatibility.json has an unsupported schema")
    return manifest


def validate_manifest(manifest: dict) -> None:
    """Validate every recorded tested version and source metadata declaration."""
    for section in ("runtime", "contributor"):
        for name, entry in manifest[section].items():
            if not isinstance(entry, dict) or "constraint" not in entry:
                continue
            for tested in entry.get("tested", ()):
                if not satisfies(tested, entry["constraint"]):
                    raise CompatibilityError(
                        f"{section}.{name} tested version {tested} violates {entry['constraint']}"
                    )

    plugin = (ROOT / "orcad.py").read_text(encoding="utf-8")
    python_constraint = manifest["runtime"]["python"]["constraint"]
    if f'# requires-python = "{python_constraint}"' not in plugin:
        raise CompatibilityError("orcad.py requires-python does not match compatibility.json")
    for package, constraint in (
        ("build123d", manifest["runtime"]["build123d"]["constraint"]),
        (manifest["runtime"]["ocp"]["distribution"], manifest["runtime"]["ocp"]["constraint"]),
        ("numpy", manifest["runtime"]["numpy"]["constraint"]),
    ):
        if f'"{package}{constraint}"' not in plugin and f'"{package}{constraint}"' not in plugin.replace(" ", ""):
            raise CompatibilityError(f"orcad.py does not declare {package}{constraint}")

    package_json = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    lock_json = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    engines = package_json.get("engines", {})
    lock_engines = lock_json.get("packages", {}).get("", {}).get("engines", {})
    for name in ("node", "npm"):
        expected = manifest["contributor"][name]["constraint"].replace(",", " ")
        if engines.get(name) != expected:
            raise CompatibilityError(f"frontend/package.json {name} engine does not match manifest")
        if lock_engines.get(name) != expected:
            raise CompatibilityError(f"frontend/package-lock.json {name} engine is out of sync")


def _command_version(command: str) -> str:
    executable = shutil.which(command)
    if executable is None:
        raise CompatibilityError(f"required contributor tool is not on PATH: {command}")
    result = subprocess.run(
        [executable, "--version"], cwd=ROOT, capture_output=True, text=True,
        timeout=30, check=False,
    )
    if result.returncode:
        raise CompatibilityError(f"{command} --version failed")
    output = (result.stdout or result.stderr).strip().splitlines()
    if not output:
        raise CompatibilityError(f"{command} returned no version")
    match = re.search(r"\d+(?:\.\d+){0,3}", output[0])
    if not match:
        raise CompatibilityError(f"{command} returned an unparseable version")
    return match.group(0)


def _check_tool(manifest: dict, name: str, command: str) -> None:
    version = _command_version(command)
    constraint = manifest["contributor"][name]["constraint"]
    if not satisfies(version, constraint):
        raise CompatibilityError(f"{command} {version} is outside declared {constraint}")
    print(f"compatibility: {command} {version} satisfies {constraint}")


def _check_python(manifest: dict) -> None:
    version = ".".join(str(part) for part in sys.version_info[:3])
    constraint = manifest["contributor"]["python"]["constraint"]
    if not satisfies(version, constraint):
        raise CompatibilityError(f"python {version} is outside declared {constraint}")
    print(f"compatibility: python {version} satisfies {constraint}")


def _check_optional_cad(manifest: dict) -> None:
    python = ROOT / ".venv" / "bin" / "python"
    if not python.is_file():
        print("compatibility: optional CAD environment not present (geometry check will skip)")
        return
    code = (
        "import importlib.metadata as m, json, sys; "
        "names = ['build123d', 'cadquery-ocp-novtk', 'numpy']; "
        "print(json.dumps({'python': '.'.join(map(str, sys.version_info[:3])), "
        "'packages': {n: m.version(n) for n in names}}))"
    )
    result = subprocess.run(
        [str(python), "-c", code], cwd=ROOT, capture_output=True, text=True,
        timeout=30, check=False,
    )
    if result.returncode:
        print("compatibility: optional CAD environment cannot report package versions (geometry check will skip)")
        return
    try:
        details = json.loads(result.stdout)
        packages = details["packages"]
        checks = (
            ("python", details["python"], manifest["runtime"]["python"]["constraint"]),
            ("build123d", packages["build123d"], manifest["runtime"]["build123d"]["constraint"]),
            ("cadquery-ocp-novtk", packages["cadquery-ocp-novtk"], manifest["runtime"]["ocp"]["constraint"]),
            ("numpy", packages["numpy"], manifest["runtime"]["numpy"]["constraint"]),
        )
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise CompatibilityError("optional CAD environment returned invalid package metadata") from exc
    for name, version, constraint in checks:
        if not satisfies(version, constraint):
            raise CompatibilityError(f".venv {name} {version} is outside declared {constraint}")
        print(f"compatibility: .venv {name} {version} satisfies {constraint}")


def _run_stage(name: str, command: list[str], cwd: Path = ROOT) -> None:
    print(f"[compatibility] {name}: {' '.join(command)}", flush=True)
    result = subprocess.run(command, cwd=cwd, check=False)
    if result.returncode:
        raise CompatibilityError(f"{name} failed with exit {result.returncode}")


def run_matrix(manifest: dict) -> None:
    """Run the manifest's required stages and the optional geometry stage."""
    _run_stage("pure Python tests", [sys.executable, "-m", "pytest", "tests/", "-q"])
    _run_stage("frontend tests", ["npm", "test"], ROOT / "frontend")
    _run_stage("frontend build", ["npm", "run", "build"], ROOT / "frontend")
    _run_stage("bundle synchronization", [sys.executable, "packaging/bundle.py", "--check"])
    _run_stage("geometry integration", [sys.executable, "verify/geometry.py"])
    print("[compatibility] matrix passed", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="run all local matrix stages")
    args = parser.parse_args(argv)
    manifest = load_manifest()
    validate_manifest(manifest)
    _check_python(manifest)
    _check_tool(manifest, "node", "node")
    _check_tool(manifest, "npm", "npm")
    _check_tool(manifest, "pytest", "pytest")
    _check_optional_cad(manifest)
    print("compatibility: matrix declared in compatibility.json")
    for stage in manifest["matrix"]:
        print(f"  {stage['status']:8} {stage['name']}: {stage['command']}")
    if args.run:
        run_matrix(manifest)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CompatibilityError, OSError, subprocess.SubprocessError) as exc:
        print(f"compatibility: ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
