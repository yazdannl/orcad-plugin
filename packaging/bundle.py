#!/usr/bin/env python3
"""Bundle objects/*.py into the single-file orcad.py Hub artifact.

Each objects/<name>.py is a RUNNABLE build123d program. Parameter variables
carry `# spec:` comments, e.g.::

    WALL = 1.2  # spec: number label=Wall unit=mm min=0.8 max=2.4 step=0.2
    GX = 2      # spec: int label=Grid X unit=u min=1 max=6 step=1
    MODE = 0    # spec: int label=Mode options=0:Basic|1:Advanced min=0 max=1 step=1
    LIP = True  # spec: bool label=Stacking lip

This script extracts the UI spec from those variables, bakes per-object
SPEC + TEMPLATE data into orcad.py, embeds the compiled frontend fallback,
and smoke-tests every object (defaults + min/max extremes must stay valid
python). The frontend is built separately with `cd frontend && npm run build`.

Single source of truth: objects/*.py. Never edit the generated regions.

Usage:
    python3 packaging/bundle.py --write   # regenerate Python + frontend spec regions
    python3 packaging/bundle.py --check   # exit 1 when out of sync (tests)
"""
import ast
import base64
import gzip
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OBJDIR = ROOT / "objects"
TARGET = ROOT / "orcad.py"
FRONTEND_TARGET = ROOT / "frontend" / "src" / "primitives.js"
PY_BEGIN = "# BEGIN BUNDLED OBJECTS"
PY_END = "# END BUNDLED OBJECTS"
FRONTEND_BEGIN = "/* BEGIN GENERATED PRIMS */"
FRONTEND_END = "/* END GENERATED PRIMS */"
HTML_BEGIN = "# BEGIN BUNDLED FRONTEND"
HTML_END = "# END BUNDLED FRONTEND"
FRONTEND_ASSET = ROOT / "frontend" / "dist" / "index.html"
SPEC_LINE_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[^#\n]*#\s*spec\s*:\s*(.+)$"
)
KEYVAL_RE = re.compile(r"(label|unit|min|max|step|options)\s*=\s*([^,]+?)(?=\s+(?:label|unit|min|max|step|options)\s*=|$)")


def _parse_options(raw, path, var, ptype):
    options = []
    seen = set()
    for item in raw.split("|"):
        if ":" not in item:
            raise ValueError(f"{path}: spec on {var} has invalid option {item!r}")
        value_src, label = item.split(":", 1)
        try:
            value = float(value_src.strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}: spec on {var} has invalid option value") from exc
        if ptype == "int":
            if not value.is_integer():
                raise ValueError(f"{path}: spec on {var} has non-integer option value")
            try:
                value = int(value)
            except (OverflowError, ValueError) as exc:
                raise ValueError(f"{path}: spec on {var} has invalid option value") from exc
        label = label.strip()
        if not label:
            raise ValueError(f"{path}: spec on {var} has an empty option label")
        if value in seen:
            raise ValueError(f"{path}: spec on {var} repeats option value {value}")
        seen.add(value)
        options.append({"value": value, "label": label})
    if not options:
        raise ValueError(f"{path}: spec on {var} needs at least one option")
    return options


def _parse_spec_body(body, path, var):
    match = re.match(r"(number|int|bool)\b\s*(.*)$", body.strip())
    if not match:
        raise ValueError(f"{path}: bad spec on {var} (need ptype first)")
    ptype, rest = match.group(1), match.group(2)
    fields = {key: val.strip() for key, val in KEYVAL_RE.findall(rest)}
    if "label" not in fields:
        raise ValueError(f"{path}: spec on {var} needs label=")
    if ptype in ("number", "int"):
        for key in ("min", "max", "step"):
            if key not in fields:
                raise ValueError(f"{path}: spec on {var} needs {key}=")
            try:
                fields[key] = float(fields[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{path}: spec on {var} has invalid {key}") from exc
        if "options" in fields:
            fields["options"] = _parse_options(fields["options"], path, var, ptype)
    elif set(fields) - {"label", "unit"}:
        raise ValueError(f"{path}: bool spec on {var} takes only label=/unit=")
    fields.setdefault("unit", "")
    return ptype, fields


def parse_object(path):
    """Return {name, label, blurb, params, source} for one object file."""
    source = path.read_text(encoding="utf-8")
    if '"""' in source:
        raise ValueError(f"{path}: triple-double-quotes break TEMPLATE embedding")
    tree = ast.parse(source, filename=str(path))
    stars = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "build123d" \
                and any(a.name == "*" for a in node.names):
            stars += 1
        elif isinstance(node, ast.Import) and [a.name for a in node.names] == ["math"]:
            pass  # stdlib, always available (e.g. crush-rib wave polygon)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError(f"{path}: only `from build123d import *` + `import math` allowed")
    if stars != 1:
        raise ValueError(f"{path}: exactly one `from build123d import *` required")
    stores = {n.id for n in ast.walk(tree)
              if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    if "result" not in stores:
        raise ValueError(f"{path}: must assign `result`")
    label = blurb = None
    for line in source.splitlines()[:15]:
        if line.startswith("# object:"):
            label = line.split(":", 1)[1].strip()
        elif line.startswith("# blurb:"):
            blurb = line.split(":", 1)[1].strip()
    if not label or not blurb:
        raise ValueError(f"{path}: need `# object:` + `# blurb:` header comments")
    params = []
    seen = set()
    for lineno, line in enumerate(source.splitlines(), start=1):
        match = SPEC_LINE_RE.match(line)
        if not match:
            continue
        var = match.group(1)
        if var in seen:
            raise ValueError(f"{path}:{lineno}: duplicate spec variable {var}")
        seen.add(var)
        value_src = line.split("=", 1)[1].split("#")[0]
        try:
            default = ast.literal_eval(value_src.strip())
        except Exception:
            raise ValueError(f"{path}:{lineno}: spec default must be a literal") from None
        ptype, fields = _parse_spec_body(match.group(2), f"{path}:{lineno}", var)
        if ptype == "bool" and not isinstance(default, bool):
            raise ValueError(f"{path}:{lineno}: bool default must be True/False")
        if ptype == "int":
            try:
                whole = float(default).is_integer()
            except (TypeError, ValueError):
                whole = False
            if not whole:
                raise ValueError(f"{path}:{lineno}: int default must be whole")
        if ptype == "number" and not isinstance(default, (int, float)):
            raise ValueError(f"{path}:{lineno}: number default must be numeric")
        if "options" in fields:
            values = {option["value"] for option in fields["options"]}
            if default not in values:
                raise ValueError(f"{path}:{lineno}: default is not one of the options")
            if any(value < fields["min"] or value > fields["max"] for value in values):
                raise ValueError(f"{path}:{lineno}: option is outside the declared range")
        param = {"key": var, "label": fields["label"], "unit": fields.get("unit", ""),
                 "ptype": ptype, "default": default}
        if ptype in ("number", "int"):
            param.update({k: fields[k] for k in ("min", "max", "step")})
            if "options" in fields:
                param["options"] = fields["options"]
        params.append(param)
    if not params:
        raise ValueError(f"{path}: no `# spec:` variables found")
    return {"name": path.stem, "label": label, "blurb": blurb,
            "params": params, "source": source}


def bake_template(template, values):
    """Substitute annotated `VAR = ...  # spec: ...` lines with chosen values."""
    out = []
    for line in template.splitlines():
        match = SPEC_LINE_RE.match(line)
        if match and match.group(1) in values:
            comment = "#" + line.split("#", 1)[1]
            line = f"{match.group(1)} = {values[match.group(1)]!r}  {comment}"
        out.append(line)
    return "\n".join(out) + "\n"


def smoke_object(parsed):
    defaults = {p["key"]: p["default"] for p in parsed["params"]}
    ast.parse(bake_template(parsed["source"], defaults))
    for key in defaults:
        for extreme in ("min", "max"):
            trial = dict(defaults)
            for p in parsed["params"]:
                if p["key"] == key and p["ptype"] in ("number", "int"):
                    trial[key] = p[extreme]
            ast.parse(bake_template(parsed["source"], trial))


def build_py_region(objects):
    chunks = []
    for obj in objects:
        smoke_object(obj)
        spec = {"label": obj["label"], "blurb": obj["blurb"], "params": obj["params"]}
        chunks.append(f"{obj['name']}_SPEC = {spec!r}")
        chunks.append(f'{obj["name"]}_TEMPLATE = r"""\n{obj["source"].rstrip()}\n"""')
    entries = ",\n".join(f'    "{o["name"]}": {o["name"]}_SPEC' for o in objects)
    templates = ",\n".join(f'    "{o["name"]}": {o["name"]}_TEMPLATE' for o in objects)
    return (
        "# Generated by `python3 packaging/bundle.py --write`.\n"
        "# Do not edit here; edit objects/*.py instead.\n"
        + "\n\n\n".join(chunks)
        + f"\n\n\nPRIMITIVES = {{\n{entries},\n}}\n"
        + f"\n\n_TEMPLATES = {{\n{templates},\n}}\n"
        + "\n\ndef generate_primitive_code(primitive, params):\n"
        + '    """Return the object program with params baked into `# spec:` lines."""\n'
        + "    c = validate_primitive_params(primitive, params)\n"
        + "    try:\n"
        + "        template = _TEMPLATES[primitive]\n"
        + "    except KeyError:\n"
        + '        raise ValueError(f"unknown object {primitive!r}") from None\n'
        + "    return _bake_template(template, c)\n"
    )


def _load_objects():
    objects = []
    for path in sorted(OBJDIR.glob("*.py")):
        if path.name.startswith("_") or path.name == "__init__.py":
            continue
        objects.append(parse_object(path))
    if not objects:
        raise ValueError("no object modules found in objects/")
    return objects


def _replace_region(text, begin, end, fresh):
    lines = text.splitlines(keepends=True)
    bi = next(i for i, line in enumerate(lines) if line.rstrip("\n") == begin)
    ei = next(i for i, line in enumerate(lines) if line.rstrip("\n") == end)
    if not bi < ei:
        raise ValueError("markers out of order")
    return "".join(lines[:bi + 1]) + fresh + "\n" + "".join(lines[ei:])


def build_frontend_asset():
    compressed = gzip.compress(FRONTEND_ASSET.read_bytes(), compresslevel=9, mtime=0)
    encoded = base64.b64encode(compressed).decode("ascii")
    lines = "\\n".join(encoded[index:index + 96] for index in range(0, len(encoded), 96))
    return f'_EMBEDDED_FRONTEND_GZIP = b"""\\n{lines}\\n"""'


def build_frontend_region(objects):
    specs = {}
    for obj in objects:
        params = []
        for p in obj["params"]:
            if p["ptype"] == "bool":
                params.append([p["key"], p["label"], p["unit"], "bool", p["default"]])
            else:
                param = [p["key"], p["label"], p["unit"], p["ptype"], p["default"],
                         p["min"], p["max"], p["step"]]
                if "options" in p:
                    param.append(p["options"])
                params.append(param)
        specs[obj["name"]] = {"label": obj["label"], "blurb": obj["blurb"], "params": params}
    return "export const PRIMS = " + json.dumps(specs, separators=(",", ":")) + ";"


def build_target():
    objects = _load_objects()
    text = TARGET.read_text(encoding="utf-8")
    text = _replace_region(text, PY_BEGIN, PY_END, build_py_region(objects))
    return _replace_region(text, HTML_BEGIN, HTML_END, build_frontend_asset())


def build_frontend_target():
    objects = _load_objects()
    text = FRONTEND_TARGET.read_text(encoding="utf-8")
    return _replace_region(text, FRONTEND_BEGIN, FRONTEND_END, build_frontend_region(objects))


def check():
    return (TARGET.read_text(encoding="utf-8") == build_target()
            and FRONTEND_TARGET.read_text(encoding="utf-8") == build_frontend_target())


def write():
    TARGET.write_text(build_target(), encoding="utf-8")
    FRONTEND_TARGET.write_text(build_frontend_target(), encoding="utf-8")


if __name__ == "__main__":
    if "--write" in sys.argv:
        write()
        print("bundled objects into orcad.py and frontend/src/primitives.js")
    elif "--check" in sys.argv:
        sys.exit(0 if check() else 1)
    else:
        sys.exit("usage: bundle.py --write | --check")
