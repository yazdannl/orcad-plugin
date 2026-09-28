#!/usr/bin/env python3
"""Build the single-file orcad.py release.

OrcaSlicer's Plugin Hub accepts one .py file, so the compiled frontend
(frontend/dist/index.html) and the OpenSCAD backend package (openscad/, with
the vendored Gridfinity library) are embedded into orcad.py as compressed,
deterministic blobs. When the package is installed as a folder, orcad.py
prefers the files beside it instead.

    python3 packaging/bundle.py            # build the frontend and embed it + the backend
    python3 packaging/bundle.py --check    # fail if orcad.py or dist/ is stale (CI)
    python3 packaging/bundle.py --release  # build, embed, then run all tests
"""
from __future__ import annotations

import argparse
import base64
import gzip
import io
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "orcad.py"
FRONTEND = ROOT / "frontend"
DIST = FRONTEND / "dist" / "index.html"
BACKEND = ROOT / "openscad"
REGIONS = {
    "backend": ("# BEGIN EMBEDDED OPENSCAD BACKEND", "# END EMBEDDED OPENSCAD BACKEND", "_EMBEDDED_BACKEND"),
    "frontend": ("# BEGIN BUNDLED FRONTEND", "# END BUNDLED FRONTEND", "_EMBEDDED_FRONTEND"),
}
_SKIP = re.compile(r"(^|/)(__pycache__|\.pytest_cache)(/|$)|\.py[co]$")


def _blob(data: bytes) -> str:
    return base64.b64encode(gzip.compress(data, compresslevel=9, mtime=0)).decode("ascii")


def backend_archive() -> bytes:
    """Deterministic zip of openscad/ (fixed timestamps, sorted names)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(BACKEND.rglob("*")):
            name = path.relative_to(ROOT).as_posix()
            if path.is_file() and not _SKIP.search(name):
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def render_target(text: str, frontend_html: str) -> str:
    values = {"backend": _blob(backend_archive()), "frontend": _blob(frontend_html.encode("utf-8"))}
    for key, (begin, end, variable) in REGIONS.items():
        pattern = re.compile(re.escape(begin) + r"\n.*?\n" + re.escape(end), re.S)
        if not pattern.search(text):
            raise SystemExit(f"{TARGET.name}: missing region {begin!r}")
        replacement = f'{begin}\n{variable} = "{values[key]}"\n{end}'
        text = pattern.sub(lambda _, r=replacement: r, text, count=1)  # a function: blobs are not regex templates
    return text


def build_frontend(out_dir: Path | None = None) -> None:
    if not (FRONTEND / "node_modules").is_dir():
        raise SystemExit("frontend dependencies are missing; run `cd frontend && npm ci` first")
    command = ["npm", "run", "build", "--"] + (["--outDir", str(out_dir), "--emptyOutDir"] if out_dir else [])
    subprocess.run(command, cwd=FRONTEND, check=True, shell=sys.platform == "win32")


def run_tests() -> None:
    subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=ROOT, check=True)
    subprocess.run(["npm", "test"], cwd=FRONTEND, check=True, shell=sys.platform == "win32")


def check() -> int:
    with tempfile.TemporaryDirectory() as directory:
        build_frontend(Path(directory))
        fresh = (Path(directory) / "index.html").read_text(encoding="utf-8")
    problems = []
    if not DIST.is_file() or DIST.read_text(encoding="utf-8") != fresh:
        problems.append("frontend/dist/index.html is stale")
    current = TARGET.read_text(encoding="utf-8")
    if render_target(current, fresh) != current:
        problems.append("orcad.py embedded blobs are stale")
    for problem in problems:
        print(f"error: {problem}; run `python3 packaging/bundle.py`", file=sys.stderr)
    return 1 if problems else 0


def write() -> None:
    build_frontend()
    TARGET.write_text(render_target(TARGET.read_text(encoding="utf-8"), DIST.read_text(encoding="utf-8")),
                      encoding="utf-8")
    shutil.rmtree(BACKEND / "__pycache__", ignore_errors=True)
    print(f"embedded {DIST.relative_to(ROOT)} and openscad/ into {TARGET.name} "
          f"({TARGET.stat().st_size // 1024} KB)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify generated files without writing")
    mode.add_argument("--release", action="store_true", help="build, embed and run every test suite")
    args = parser.parse_args()
    if args.check:
        return check()
    write()
    if args.release:
        run_tests()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
