#!/usr/bin/env python3
"""Compare two STL meshes with explicit dimensional and hole constraints.

Usage: w_compare.py REF.stl OURS.stl [--heights z1,z2,...] [--holes z1,z2,...]
       [--tol-dim 0.3] [--tol-vol 0.04]

Hole loops are matched one-to-one by center and equivalent radius.  A loose
"every reference hole has some nearby hole" check is intentionally not used:
missing, extra, position, and size failures are reported separately.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

import numpy as np


def read_stl(path):
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read STL {path}: {exc}") from exc
    if len(raw) < 84:
        raise ValueError(f"STL is truncated: {path}")
    if raw[:5].lower() == b"solid" and b"facet" in raw[:500]:
        tris = []
        for block in raw.split(b"facet normal")[1:]:
            pts = []
            for line in block.split(b"\n"):
                line = line.strip()
                if line.startswith(b"vertex"):
                    pts.append([float(x) for x in line.split()[1:4]])
            if len(pts) == 3:
                tris.append(pts)
        if not tris:
            raise ValueError(f"ASCII STL contains no triangles: {path}")
        return np.asarray(tris, dtype=np.float64)
    ntri = struct.unpack("<I", raw[80:84])[0]
    expected = 84 + ntri * 50
    if expected > len(raw):
        raise ValueError(f"binary STL is truncated: {path} (needs {expected} bytes)")
    rec = np.frombuffer(raw, dtype=np.uint8, offset=84, count=ntri * 50).reshape(ntri, 50)
    floats = np.frombuffer(rec[:, :48].tobytes(), dtype="<f4").reshape(ntri, 4, 3)
    tris = floats.astype(np.float64)[:, 1:, :]
    if not len(tris):
        raise ValueError(f"STL contains no triangles: {path}")
    return tris


def metrics(tris):
    if not len(tris):
        raise ValueError("mesh contains no triangles")
    v0, v1, v2 = tris[:, 0], tris[:, 1], tris[:, 2]
    vol = abs(np.sum(np.einsum("ij,ij->i", v0, np.cross(v1, v2))) / 6.0)
    area = 0.5 * np.sum(np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1))
    return {"bbox_min": tris.min(axis=(0, 1)).tolist(),
            "bbox_max": tris.max(axis=(0, 1)).tolist(),
            "volume": float(vol), "area": float(area), "tris": int(len(tris))}


def slice_segments(tris, z):
    d = tris[:, :, 2] - z
    above = d > 1e-9
    pts = []
    for k in np.nonzero((above.sum(axis=1) == 1) | (above.sum(axis=1) == 2))[0]:
        zs = tris[k, :, 2]
        crossings = []
        for i in range(3):
            j = (i + 1) % 3
            if (zs[i] - z) * (zs[j] - z) < 0:
                a, b = tris[k, i], tris[k, j]
                t = (z - a[2]) / (b[2] - a[2])
                crossings.append(a + t * (b - a))
        if len(crossings) == 2:
            pts.append((crossings[0], crossings[1]))
    return pts


def trace_loops(segs, eps=0.05):
    rnd = lambda p: (round(round(p[0] / eps) * eps, 3), round(round(p[1] / eps) * eps, 3))
    unused = [(rnd(a), rnd(b)) for a, b in segs]
    loops = []
    while unused:
        start, cur = unused.pop()
        loop = [start]
        for _ in range(len(unused) + 1):
            nxt = None
            for i, (a, b) in enumerate(unused):
                if a == cur:
                    nxt = (i, b)
                    break
                if b == cur:
                    nxt = (i, a)
                    break
            if nxt is None:
                break
            i, cur = nxt
            unused.pop(i)
            loop.append(cur)
            if cur == start:
                if len(loop) > 2:
                    loops.append(loop)
                break
    return loops


def shoelace(loop):
    return abs(sum(loop[i][0] * loop[i + 1][1] - loop[i + 1][0] * loop[i][1]
                   for i in range(len(loop) - 1))) / 2.0


def holes_at(tris, z, area_limit=500):
    out = []
    for loop in trace_loops(slice_segments(tris, z)):
        area = shoelace(loop)
        if area < area_limit:
            xs = [p[0] for p in loop]
            ys = [p[1] for p in loop]
            out.append({"center": [round((min(xs) + max(xs)) / 2, 3),
                                   round((min(ys) + max(ys)) / 2, 3)],
                        "area": round(area, 2),
                        "radius": round((area / np.pi) ** 0.5, 3)})
    return sorted(out, key=lambda h: (h["center"][0], h["center"][1]))


def profile_at(tris, z):
    pts = [p for seg in slice_segments(tris, z) for p in seg]
    if not pts:
        return None
    pts = np.array(pts)
    return [round(float(pts[:, 0].max() - pts[:, 0].min()), 3),
            round(float(pts[:, 1].max() - pts[:, 1].min()), 3)]


def match_holes(reference, ours, center_tol=0.4, radius_tol=0.25):
    """Return a maximum-cardinality one-to-one hole matching.

    The small bipartite graph is solved with an augmenting-path matcher, so
    two reference holes cannot both claim the same output hole.
    """
    candidates = []
    for ref in reference:
        row = []
        for index, got in enumerate(ours):
            center = float(np.hypot(ref["center"][0] - got["center"][0],
                                    ref["center"][1] - got["center"][1]))
            radius = abs(ref["radius"] - got["radius"])
            if center <= center_tol and radius <= radius_tol:
                row.append((center, radius, index))
        candidates.append(sorted(row))

    assigned = {}

    def augment(ref_index, seen):
        for _, _, ours_index in candidates[ref_index]:
            if ours_index in seen:
                continue
            seen.add(ours_index)
            if ours_index not in assigned or augment(assigned[ours_index], seen):
                assigned[ours_index] = ref_index
                return True
        return False

    for index in sorted(range(len(reference)), key=lambda i: len(candidates[i])):
        augment(index, set())
    ref_to_ours = {ref_index: ours_index for ours_index, ref_index in assigned.items()}
    matches = []
    for ref_index, ours_index in sorted(ref_to_ours.items()):
        ref, got = reference[ref_index], ours[ours_index]
        matches.append({"ref": ref, "ours": got,
                        "center_diff": round(float(np.hypot(
                            ref["center"][0] - got["center"][0],
                            ref["center"][1] - got["center"][1])), 3),
                        "radius_diff": round(abs(ref["radius"] - got["radius"]), 3)})
    missing = [h for i, h in enumerate(reference) if i not in ref_to_ours]
    extra = [h for i, h in enumerate(ours) if i not in assigned]
    # Explain otherwise-position-correct holes that failed only the size check.
    size_mismatch = []
    for ref in missing:
        nearby = [got for got in ours if float(np.hypot(
            ref["center"][0] - got["center"][0],
            ref["center"][1] - got["center"][1])) <= center_tol]
        if nearby and all(abs(ref["radius"] - got["radius"]) > radius_tol for got in nearby):
            size_mismatch.append(ref)
    return {"matches": matches, "missing": missing, "extra": extra,
            "size_mismatch": size_mismatch}


def parse_heights(value):
    return [float(x) for x in value.split(",")] if value else []


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference")
    parser.add_argument("ours")
    parser.add_argument("--heights", default="")
    parser.add_argument("--holes", default="")
    parser.add_argument("--tol-dim", type=float, default=0.3)
    parser.add_argument("--tol-vol", type=float, default=0.04)
    parser.add_argument("--tol-hole-center", type=float, default=0.4)
    parser.add_argument("--tol-hole-radius", type=float, default=0.25,
                        help="maximum equivalent-radius difference in mm")
    parser.add_argument("--hole-area-limit", type=float, default=500)
    return parser.parse_args(argv)


def compare(args):
    if min(args.tol_dim, args.tol_vol, args.tol_hole_center, args.tol_hole_radius) < 0:
        raise ValueError("comparison tolerances cannot be negative")
    ref, ours = read_stl(args.reference), read_stl(args.ours)
    mr, mo = metrics(ref), metrics(ours)
    fails = []
    out = {"ref": mr, "ours": mo, "profiles": [], "holes": []}

    bb_dim_r = np.subtract(mr["bbox_max"], mr["bbox_min"])
    bb_dim_o = np.subtract(mo["bbox_max"], mo["bbox_min"])
    out["bbox_diff"] = [round(float(x), 3) for x in (bb_dim_o - bb_dim_r)]
    out["bbox_min_diff"] = [round(float(x), 3) for x in np.subtract(mo["bbox_min"], mr["bbox_min"])]
    if max(abs(x) for x in out["bbox_diff"]) > args.tol_dim:
        fails.append(f"bbox dims differ {out['bbox_diff']}")
    if max(abs(x) for x in out["bbox_min_diff"]) > args.tol_dim:
        fails.append(f"bbox min differs {out['bbox_min_diff']}")
    if mr["volume"] == 0:
        raise ValueError("reference mesh has zero volume")
    out["vol_rel_diff"] = round(abs(mo["volume"] - mr["volume"]) / mr["volume"], 4)
    if out["vol_rel_diff"] > args.tol_vol:
        fails.append(f"volume rel diff {out['vol_rel_diff']}")

    for z in parse_heights(args.heights):
        pr, po = profile_at(ref, z), profile_at(ours, z)
        row = {"z": z, "ref": pr, "ours": po}
        if pr is None or po is None:
            row["diff"] = None
            if pr != po:
                fails.append(f"profile z={z}: one side empty")
        else:
            row["diff"] = [round(abs(a - b), 3) for a, b in zip(po, pr)]
            if max(row["diff"]) > args.tol_dim:
                fails.append(f"profile z={z} differs {row['diff']}")
        out["profiles"].append(row)

    for z in parse_heights(args.holes):
        hr = holes_at(ref, z, args.hole_area_limit)
        ho = holes_at(ours, z, args.hole_area_limit)
        row = {"z": z, "ref": hr, "ours": ho,
               **match_holes(hr, ho, args.tol_hole_center, args.tol_hole_radius)}
        if row["missing"] or row["extra"]:
            fails.append(f"holes z={z}: missing={len(row['missing'])} extra={len(row['extra'])}"
                         + (f" size_mismatch={len(row['size_mismatch'])}" if row["size_mismatch"] else ""))
        out["holes"].append(row)

    out["pass"] = not fails
    out["fails"] = fails
    return out


def main(argv=None):
    try:
        out = compare(parse_args(argv))
    except (OSError, ValueError, struct.error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1))
    return 0 if out["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
