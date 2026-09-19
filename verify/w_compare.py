#!/usr/bin/env python3
"""Compare two STLs: bbox, volume, area, z-profile widths, hole loops.

Usage: w_compare.py REF.stl OURS.stl [--heights z1,z2,...] [--holes z1,z2,...]
       [--tol-dim 0.3] [--tol-vol 0.04]

- z-profiles: at each height, intersect mesh with plane -> bbox of points.
- hole loops: at each --holes height, trace closed intersection loops;
  loops with area < 500mm^2 are reported as holes (center, area, equiv radius).
- exit 0 when bbox/z-profiles within tol-dim, volume within tol-vol, and every
  reference hole loop has a matching loop in ours (center <= 0.4mm).
Triangle counts are reported, never compared (different kernels tessellate
differently — identical STLs are physically impossible).
"""
import json
import struct
import sys

import numpy as np


def read_stl(path):
    raw = open(path, "rb").read()
    if raw[:5] == b"solid" and b"facet" in raw[:500]:
        tris = []
        for block in raw.split(b"facet normal")[1:]:
            pts = []
            for line in block.split(b"\n"):
                line = line.strip()
                if line.startswith(b"vertex"):
                    pts.append([float(x) for x in line.split()[1:4]])
            if len(pts) == 3:
                tris.append(pts)
        return np.asarray(tris, dtype=np.float64)
    ntri = struct.unpack("<I", raw[80:84])[0]
    rec = np.frombuffer(raw, dtype=np.uint8, offset=84, count=ntri * 50).reshape(ntri, 50)
    floats = np.frombuffer(rec[:, :48].tobytes(), dtype="<f4").reshape(ntri, 4, 3)
    return floats.astype(np.float64)[:, 1:, :]


def metrics(tris):
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
        order = np.argsort(zs)
        # find the two crossing edges explicitly
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
    # eps-snapping: OCC STL writers emit per-face triangulations whose shared
    # edges meet at T-junctions ~0.02mm apart (export tolerance); snap them so
    # loops close. eps is far below the smallest real feature (0.6 dividers).
    rnd = lambda p: (round(round(p[0] / eps) * eps, 3), round(round(p[1] / eps) * eps, 3))
    unused = [(rnd(a), rnd(b)) for a, b in segs]
    loops = []
    while unused:
        start, cur = unused.pop()
        loop = [start]
        closed = False
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
                closed = True
                break
        if closed and len(loop) > 2:
            loops.append(loop)
    return loops


def shoelace(loop):
    s = 0.0
    for i in range(len(loop) - 1):
        s += loop[i][0] * loop[i + 1][1] - loop[i + 1][0] * loop[i][1]
    return abs(s) / 2.0


def holes_at(tris, z):
    out = []
    for loop in trace_loops(slice_segments(tris, z)):
        area = shoelace(loop)
        if area < 500:
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


def main():
    args = sys.argv[1:]
    ref_path, ours_path = args[0], args[1]
    heights = [float(x) for x in args[args.index("--heights") + 1].split(",")] \
        if "--heights" in args else []
    hole_zs = [float(x) for x in args[args.index("--holes") + 1].split(",")] \
        if "--holes" in args else []
    tol_dim = float(args[args.index("--tol-dim") + 1]) if "--tol-dim" in args else 0.3
    tol_vol = float(args[args.index("--tol-vol") + 1]) if "--tol-vol" in args else 0.04

    ref, ours = read_stl(ref_path), read_stl(ours_path)
    mr, mo = metrics(ref), metrics(ours)
    fails = []
    out = {"ref": mr, "ours": mo, "profiles": [], "holes": []}

    bb_dim_r = np.subtract(mr["bbox_max"], mr["bbox_min"])
    bb_dim_o = np.subtract(mo["bbox_max"], mo["bbox_min"])
    out["bbox_diff"] = [round(float(x), 3) for x in (bb_dim_o - bb_dim_r)]
    out["bbox_min_diff"] = [round(float(x), 3) for x in
                            np.subtract(mo["bbox_min"], mr["bbox_min"])]
    if max(abs(x) for x in out["bbox_diff"]) > tol_dim:
        fails.append(f"bbox dims differ {out['bbox_diff']}")
    if max(abs(x) for x in out["bbox_min_diff"]) > tol_dim:
        fails.append(f"bbox min differs {out['bbox_min_diff']}")
    out["vol_rel_diff"] = round(abs(mo["volume"] - mr["volume"]) / mr["volume"], 4)
    if out["vol_rel_diff"] > tol_vol:
        fails.append(f"volume rel diff {out['vol_rel_diff']}")

    for z in heights:
        pr, po = profile_at(ref, z), profile_at(ours, z)
        row = {"z": z, "ref": pr, "ours": po}
        if pr is None or po is None:
            row["diff"] = None
            if pr != po:
                fails.append(f"profile z={z}: one side empty")
        else:
            row["diff"] = [round(abs(a - b), 3) for a, b in zip(po, pr)]
            if max(row["diff"]) > tol_dim:
                fails.append(f"profile z={z} differs {row['diff']}")
        out["profiles"].append(row)

    for z in hole_zs:
        hr, ho = holes_at(ref, z), holes_at(ours, z)
        row = {"z": z, "ref": hr, "ours": ho, "missing": []}
        for h in hr:
            best = min([np.hypot(h["center"][0] - o["center"][0],
                                 h["center"][1] - o["center"][1]) for o in ho] or [1e9])
            if best > 0.4:
                row["missing"].append(h)
        for h in ho:
            best = min([np.hypot(h["center"][0] - o["center"][0],
                                 h["center"][1] - o["center"][1]) for o in hr] or [1e9])
            if best > 0.4:
                row.setdefault("extra", []).append(h)
        if row["missing"] or row.get("extra"):
            fails.append(f"holes z={z}: missing={len(row['missing'])} extra={len(row.get('extra', []))}")
        out["holes"].append(row)

    out["pass"] = not fails
    out["fails"] = fails
    print(json.dumps(out, indent=1))
    return 0 if out["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
