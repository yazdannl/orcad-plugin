#!/usr/bin/env python3
"""Bundle objects/*.py into the single-file orcad.py Hub artifact.

Each objects/<name>.py is a RUNNABLE build123d program. Parameter variables
carry `# spec:` comments, e.g.::

    WALL = 1.2  # spec: number label=Wall unit=mm min=0.8 max=2.4 step=0.2
    GX = 2      # spec: int label=Grid X unit=u min=1 max=6 step=1
    LIP = True  # spec: bool label=Stacking lip

This script extracts the UI spec from those variables, bakes per-object
SPEC + TEMPLATE + the JS PRIMS block into orcad.py, and smoke-tests every
object (defaults + min/max extremes must stay valid python).

Single source of truth: objects/*.py. Never edit the generated regions.

Usage:
    python3 packaging/bundle.py --write   # regenerate orcad.py regions
    python3 packaging/bundle.py --check   # exit 1 when out of sync (tests)
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OBJDIR = ROOT / "objects"
TARGET = ROOT / "orcad.py"
PY_BEGIN = "# BEGIN BUNDLED OBJECTS"
PY_END = "# END BUNDLED OBJECTS"
JS_BEGIN = "/* BEGIN OBJECTS SPEC */"
JS_END = "/* END OBJECTS SPEC */"

SPEC_LINE_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[^#\n]*#\s*spec\s*:\s*(.+)$"
)
KEYVAL_RE = re.compile(r"(label|unit|min|max|step)\s*=\s*([^,]+?)(?=\s+(?:label|unit|min|max|step)\s*=|$)")


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
        param = {"key": var, "label": fields["label"], "unit": fields.get("unit", ""),
                 "ptype": ptype, "default": default}
        if ptype in ("number", "int"):
            param.update({k: fields[k] for k in ("min", "max", "step")})
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


def _js_str(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


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


def build_js_block(objects):
    entries = []
    for obj in objects:
        rendered = []
        for p in obj["params"]:
            if p["ptype"] == "bool":
                rendered.append(f"[{_js_str(p['key'])},{_js_str(p['label'])},"
                                f"{_js_str(p['unit'])},'bool',{1 if p['default'] else 0}]")
            else:
                default = p["default"]
                rendered.append(f"[{_js_str(p['key'])},{_js_str(p['label'])},"
                                f"{_js_str(p['unit'])},'{p['ptype']}',{default},"
                                f"{p['min']},{p['max']},{p['step']}]")
        entries.append(f" {obj['name']}:{{label:{_js_str(obj['label'])},"
                       f"blurb:{_js_str(obj['blurb'])},params:[{','.join(rendered)}]}}")
    return "var PRIMS={\n" + ",\n".join(entries) + "\n};"


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


def build_target():
    objects = _load_objects()
    text = TARGET.read_text(encoding="utf-8")
    text = _replace_region(text, PY_BEGIN, PY_END, build_py_region(objects))
    text = _replace_region(text, JS_BEGIN, JS_END, build_js_block(objects))
    return text


def check():
    return TARGET.read_text(encoding="utf-8") == build_target()


def write():
    TARGET.write_text(build_target(), encoding="utf-8")


if __name__ == "__main__":
    if "--write" in sys.argv:
        write()
        print("bundled objects into orcad.py")
    elif "--check" in sys.argv:
        sys.exit(0 if check() else 1)
    else:
        sys.exit("usage: bundle.py --write | --check")
