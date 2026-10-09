#!/usr/bin/env python3
"""Render a small preview thumbnail for every catalog object.

    xvfb-run -a python3 dev/thumbs.py            # regenerate frontend/src/thumbs/
    xvfb-run -a python3 dev/thumbs.py --only box # one object, for a quick look

Each object is rendered with its catalog default parameters and the same
OpenSCAD build, library path and defines the plugin itself uses, from a fixed
isometric camera. The flat background OpenSCAD paints is keyed out (color to
alpha) and the result is cropped to the model, so a thumbnail can sit on the
panel background in either theme. OpenSCAD's PNG export needs an OpenGL
context: without a display it fails with "Unable to obtain GL Context", hence
xvfb-run.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openscad import catalog, runner  # noqa: E402

OUT = ROOT / "frontend" / "src" / "thumbs"
TMP = Path(os.environ.get("ORCAD_THUMB_TMP", "/tmp/orcad-thumbs"))
CAMERA = "0,0,0,55,0,25,0"  # isometric-ish: the model is centred automatically
MARGIN = 3
MAX_COLORS = 64


def find_openscad() -> str:
    candidates = [os.environ.get("OPENSCAD"), "/usr/bin/openscad"]
    candidates += sorted((Path.home() / ".cache" / "orcad" / "openscad").glob("*/squashfs-root/usr/bin/openscad"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise SystemExit("no OpenSCAD found; set OPENSCAD=/path/to/openscad")


def render(key: str, executable: str, version: str | None, scheme: str, size: str) -> Path:
    source = catalog.source_path(key)
    defines = {**catalog.defaults(key), "$fa": catalog.QUALITY_PROFILES["draft"]["fa"],
               "$fs": catalog.QUALITY_PROFILES["draft"]["fs"]}
    target = TMP / f"{key}.png"
    argv = [executable, "-o", str(target), f"--imgsize={size}", "--projection=o", "--autocenter", "--viewall",
            f"--camera={CAMERA}", f"--colorscheme={scheme}", *runner.backend_args(version)]
    for name, value in defines.items():
        argv += ["-D", runner.encode_define(name, value)]
    argv.append(str(source))
    result = subprocess.run(argv, cwd=source.parent, env=runner.child_env(), capture_output=True, text=True,
                            timeout=1800)
    if result.returncode != 0 or not target.is_file():
        detail = (result.stderr or result.stdout).strip().splitlines()
        reason = detail[-1] if detail else f"exit code {result.returncode}"
        if "GL Context" in reason:
            reason += " (run this under xvfb-run)"
        raise SystemExit(f"{key}: render failed: {reason}")
    return target


def image_to_alpha(path: Path, out: Path) -> tuple[int, int]:
    """Key out the flat render background, crop to the model, save a small PNG."""
    from PIL import Image

    image = Image.open(path).convert("RGBA")
    pixels = image.load()
    width, height = image.size
    background = pixels[0, 0][:3]
    for y in range(height):
        for x in range(width):
            red, green, blue, _ = pixels[x, y]
            # OpenSCAD antialiases onto the background: alpha is how far the pixel
            # moved away from it (the object is darker than the background in at
            # least one channel), and the colour is un-blended back out of it.
            alpha = 1.0 - min(red / max(background[0], 1), green / max(background[1], 1),
                              blue / max(background[2], 1))
            if alpha <= 0.004:
                pixels[x, y] = (0, 0, 0, 0)
                continue
            alpha = min(alpha, 1.0)
            channels = tuple(max(0, min(255, round((value - (1.0 - alpha) * base) / alpha)))
                             for value, base in zip((red, green, blue), background))
            pixels[x, y] = (*channels, round(alpha * 255))

    box = image.getbbox()
    if box is None:
        raise SystemExit(f"{path.name}: the render is empty")
    cropped = image.crop((max(0, box[0] - MARGIN), max(0, box[1] - MARGIN),
                          min(width, box[2] + MARGIN), min(height, box[3] + MARGIN)))
    cropped.quantize(colors=MAX_COLORS, method=Image.FASTOCTREE).save(out, optimize=True)
    return cropped.size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", action="append", default=[], help="object key (repeatable)")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--scheme", default="Cornfield", help="OpenSCAD color scheme")
    parser.add_argument("--size", default="160,120", help="render size, e.g. 160,120")
    args = parser.parse_args()

    keys = args.only or sorted(catalog.CATALOG["objects"])
    unknown = [key for key in keys if key not in catalog.CATALOG["objects"]]
    if unknown:
        raise SystemExit(f"unknown object(s): {', '.join(unknown)}")

    executable = find_openscad()
    version = runner.probe_openscad(executable).version
    TMP.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    total = 0

    def build(key: str) -> str:
        rendered = render(key, executable, version, args.scheme, args.size)
        target = OUT / f"{key}.png"
        width, height = image_to_alpha(rendered, target)
        return f"{key:34} {target.stat().st_size / 1024:5.1f} KB  {width}x{height}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for message in pool.map(build, keys):
            print(message)
            total += (OUT / f"{message.split()[0]}.png").stat().st_size
    print(f"{len(keys)} thumbnail(s), {total / 1024:.0f} KB total, {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
