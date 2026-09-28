"""Binary STL helpers: validation, compact indexed preview meshes, and 3MF export."""
from __future__ import annotations

import struct
import sys
import zipfile
from array import array
from dataclasses import dataclass

from .errors import BackendError, ErrorCode

_HEADER = 84
_RECORD = 50  # normal (12) + three vertices (36) + attribute (2)


@dataclass(frozen=True)
class IndexedMesh:
    positions: array  # float32 xyz triples
    indices: array    # uint32 vertex indices, three per triangle

    @property
    def triangle_count(self) -> int:
        return len(self.indices) // 3


def triangle_count(data: bytes) -> int:
    """Return the triangle count of a well-formed, non-empty binary STL."""
    if len(data) < _HEADER:
        raise BackendError(ErrorCode.INVALID_STL, "OpenSCAD produced a truncated STL")
    (count,) = struct.unpack_from("<I", data, 80)
    if count == 0:
        raise BackendError(ErrorCode.INVALID_STL, "The model is empty")
    if len(data) < _HEADER + count * _RECORD:
        raise BackendError(ErrorCode.INVALID_STL, "OpenSCAD produced a truncated STL")
    return count


def index_stl(data: bytes) -> IndexedMesh:
    """Deduplicate vertices by their exact bytes (fast; no float round-trip)."""
    count = triangle_count(data)
    lookup: dict[bytes, int] = {}
    raw = bytearray()
    indices = array("I")
    for triangle in range(count):
        offset = _HEADER + triangle * _RECORD + 12
        for corner in range(3):
            key = data[offset + corner * 12: offset + corner * 12 + 12]
            index = lookup.get(key)
            if index is None:
                index = lookup[key] = len(lookup)
                raw += key
            indices.append(index)
    positions = array("f")
    positions.frombytes(bytes(raw))
    if sys.byteorder == "big":
        positions.byteswap()
    return IndexedMesh(positions, indices)


def write_3mf(mesh: IndexedMesh, path) -> None:
    """Write a single-object 3MF (millimetres) that slicers import directly."""
    p, t = mesh.positions, mesh.indices
    vertices = "".join(
        f'<vertex x="{p[i]:.6g}" y="{p[i + 1]:.6g}" z="{p[i + 2]:.6g}"/>' for i in range(0, len(p), 3))
    # 3MF forbids degenerate triangles (a repeated vertex index); STL tolerates them.
    triangles = "".join(
        f'<triangle v1="{t[i]}" v2="{t[i + 1]}" v3="{t[i + 2]}"/>' for i in range(0, len(t), 3)
        if t[i] != t[i + 1] and t[i + 1] != t[i + 2] and t[i] != t[i + 2])
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<resources><object id="1" type="model"><mesh><vertices>{vertices}</vertices>'
        f'<triangles>{triangles}</triangles></mesh></object></resources>'
        '<build><item objectid="1"/></build></model>')
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        '</Types>')
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("3D/3dmodel.model", model)
