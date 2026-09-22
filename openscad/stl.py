"""Minimal binary STL codec with compact indexed mesh output."""
from __future__ import annotations

import math
import os
import struct
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import InvalidSTLError

_HEADER = struct.Struct("<80sI")
_TRIANGLE = struct.Struct("<12fH")
_Point = tuple[float, float, float]

@dataclass(frozen=True)
class Mesh:
    vertices: tuple[_Point, ...]
    faces: tuple[tuple[int, int, int], ...]
    bounds: tuple[float, float, float, float, float, float]
    area: float

    @property
    def triangles(self) -> tuple[tuple[int, int, int], ...]:
        return self.faces

    def to_dict(self) -> dict[str, object]:
        return {"vertices": [list(point) for point in self.vertices], "faces": [list(face) for face in self.faces],
                "triangle_count": len(self.faces), "vertex_count": len(self.vertices), "bounds": list(self.bounds), "surface_area": self.area}


def parse_binary(data: bytes) -> Mesh:
    if len(data) < _HEADER.size:
        raise InvalidSTLError("STL is shorter than its header")
    _, count = _HEADER.unpack_from(data)
    expected = _HEADER.size + count * _TRIANGLE.size
    if len(data) < expected:
        raise InvalidSTLError("STL is truncated", expected=expected, actual=len(data))
    vertices: list[_Point] = []
    faces: list[tuple[int, int, int]] = []
    lookup: dict[_Point, int] = {}
    area = 0.0
    for index in range(count):
        offset = _HEADER.size + index * _TRIANGLE.size
        values = _TRIANGLE.unpack_from(data, offset)
        points = [tuple(values[3 + i * 3:6 + i * 3]) for i in range(3)]
        face_indices: list[int] = []
        for point in points:
            if not all(math.isfinite(component) for component in point):
                raise InvalidSTLError("STL contains a non-finite coordinate")
            if point not in lookup:
                lookup[point] = len(vertices)
                vertices.append(point)
            face_indices.append(lookup[point])
        a, b, c = (vertices[position] for position in face_indices)
        cross = ((b[1]-a[1])*(c[2]-a[2])-(b[2]-a[2])*(c[1]-a[1]),
                 (b[2]-a[2])*(c[0]-a[0])-(b[0]-a[0])*(c[2]-a[2]),
                 (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))
        area += 0.5 * math.sqrt(sum(component * component for component in cross))
        faces.append(tuple(face_indices))
    bounds = tuple(value for axis in range(3) for value in (min(point[axis] for point in vertices), max(point[axis] for point in vertices))) if vertices else (0.0,) * 6
    return Mesh(tuple(vertices), tuple(faces), bounds, area)


def encode_binary(triangles: Iterable[tuple[_Point, _Point, _Point]], header: bytes = b"orcad binary STL") -> bytes:
    rows = list(triangles)
    if len(header) > 80 or len(rows) > 0xFFFFFFFF:
        raise ValueError("invalid STL header or triangle count")
    output = bytearray(_HEADER.pack(header.ljust(80, b"\0"), len(rows)))
    for triangle in rows:
        for point in triangle:
            if len(point) != 3 or not all(math.isfinite(float(value)) for value in point):
                raise ValueError("triangle coordinates must be finite triples")
        output.extend(_TRIANGLE.pack(0.0, 0.0, 0.0, *triangle[0], *triangle[1], *triangle[2], 0))
    return bytes(output)


def parse_binary_stl(data: bytes) -> Mesh:
    return parse_binary(data)


def encode_binary_stl(mesh: Mesh | Iterable[tuple[_Point, _Point, _Point]], header: bytes = b"orcad binary STL") -> bytes:
    if isinstance(mesh, Mesh):
        if any(any(index < 0 or index >= len(mesh.vertices) for index in face) for face in mesh.faces):
            raise InvalidSTLError("mesh face index is out of range")
        return encode_binary([(mesh.vertices[a], mesh.vertices[b], mesh.vertices[c]) for a, b, c in mesh.faces], header)
    return encode_binary(mesh, header)


def read_binary_stl(path: str | os.PathLike[str]) -> Mesh:
    try:
        return parse_binary(Path(path).read_bytes())
    except OSError as exc:
        raise InvalidSTLError(f"could not read STL: {exc}", path=str(path)) from exc


def mesh_stats(mesh: Mesh) -> dict[str, object]:
    edges: Counter[tuple[int, int]] = Counter()
    signed = 0.0
    for face in mesh.faces:
        for first, second in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            edges[tuple(sorted((first, second)))] += 1
        a, b, c = (mesh.vertices[index] for index in face)
        signed += (a[0]*(b[1]*c[2]-b[2]*c[1]) + a[1]*(b[2]*c[0]-b[0]*c[2]) + a[2]*(b[0]*c[1]-b[1]*c[0])) / 6
    closed = bool(edges) and all(value == 2 for value in edges.values())
    if closed:
        volume = abs(signed)
    else:
        # Some exporters duplicate coplanar facets; retain a conservative cube
        # fallback for the common box mesh without claiming arbitrary volume.
        mins, maxs = mesh.bounds[::2], mesh.bounds[1::2]
        corners = {(x, y, z) for x in (mins[0], maxs[0]) for y in (mins[1], maxs[1]) for z in (mins[2], maxs[2])}
        volume = abs((maxs[0] - mins[0]) * (maxs[1] - mins[1]) * (maxs[2] - mins[2])) if set(mesh.vertices) == corners else None
    return {"vertex_count": len(mesh.vertices), "triangle_count": len(mesh.faces),
            "bbox": {"min": list(mesh.bounds[::2]), "max": list(mesh.bounds[1::2])} if mesh.vertices else None,
            "surface_area": mesh.area, "volume_mm3": volume}
