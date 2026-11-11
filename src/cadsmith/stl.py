"""Read STL files, binary or ASCII, into a vertex and face array with shared vertices."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_RECORD = np.dtype([("normal", "<f4", 3), ("corners", "<f4", (3, 3)), ("attribute", "<u2")])


class StlError(ValueError):
    """The file is not an STL this reader understands."""


@dataclass(frozen=True)
class StlInfo:
    """What a person needs to know about an uploaded mesh."""

    triangles: int
    size: tuple[float, float, float]
    open_edges: int

    def to_dict(self) -> dict:
        return {"triangles": self.triangles, "size": [round(v, 2) for v in self.size], "open_edges": self.open_edges}


def _corners(data: bytes) -> np.ndarray:
    """Every triangle's three corners, shape (n, 3, 3)."""
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if count and 84 + count * _RECORD.itemsize == len(data):
            return np.frombuffer(data, dtype=_RECORD, count=count, offset=84)["corners"].astype(float)
    text = data.decode("ascii", errors="ignore")
    if not text.lstrip().lower().startswith("solid"):
        raise StlError("not an STL file: neither binary nor ASCII STL")
    numbers = [line.split()[1:4] for line in text.splitlines() if line.strip().lower().startswith("vertex")]
    if not numbers or len(numbers) % 3:
        raise StlError("the ASCII STL has no complete triangles")
    try:
        return np.array(numbers, dtype=float).reshape(-1, 3, 3)
    except ValueError as error:
        raise StlError(f"the ASCII STL has a bad vertex: {error}") from None


def parse_stl(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Vertices (n, 3) and faces (m, 3), with coincident corners merged and flat triangles dropped."""
    corners = _corners(data)
    if not len(corners):
        raise StlError("the STL has no triangles")
    if not np.isfinite(corners).all():
        raise StlError("the STL has coordinates that are not numbers")
    vertices, inverse = np.unique(np.round(corners.reshape(-1, 3), 5), axis=0, return_inverse=True)
    faces = np.asarray(inverse).reshape(-1, 3)
    keep = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
    if not keep.any():
        raise StlError("every triangle in the STL is flat")
    return vertices, faces[keep]


def read_stl(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    return parse_stl(Path(path).read_bytes())


def open_edges(faces: np.ndarray) -> int:
    """Edges used by only one triangle: a closed, printable mesh has none."""
    edges = np.sort(np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    return int((counts == 1).sum())


def describe_stl(data: bytes) -> StlInfo:
    vertices, faces = parse_stl(data)
    size = vertices.max(axis=0) - vertices.min(axis=0)
    return StlInfo(triangles=len(faces), size=tuple(float(v) for v in size), open_edges=open_edges(faces))
