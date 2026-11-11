"""Triangle meshes tessellated from CadQuery shapes."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from OCP.BRep import BRep_Tool
from OCP.TopAbs import TopAbs_REVERSED
from OCP.TopLoc import TopLoc_Location

LINEAR_TOLERANCE = 0.02
ANGULAR_TOLERANCE = 0.1


@dataclass
class Mesh:
    vertices: np.ndarray
    faces: np.ndarray

    @property
    def triangles(self) -> np.ndarray:
        return self.vertices[self.faces]

    def _cross(self) -> np.ndarray:
        tri = self.triangles
        return np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])

    @property
    def areas(self) -> np.ndarray:
        return 0.5 * np.linalg.norm(self._cross(), axis=1)

    @property
    def normals(self) -> np.ndarray:
        cross = self._cross()
        length = np.linalg.norm(cross, axis=1, keepdims=True)
        return cross / np.where(length == 0, 1.0, length)

    @property
    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        return self.vertices.min(axis=0), self.vertices.max(axis=0)

    @property
    def signed_volume(self) -> float:
        tri = self.triangles
        return float(np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)

    def rotated(self, rotation: np.ndarray) -> "Mesh":
        return Mesh(self.vertices @ np.asarray(rotation).T, self.faces)

    def translated(self, offset) -> "Mesh":
        return Mesh(self.vertices + np.asarray(offset, dtype=float), self.faces)


def tessellate(shape, tolerance: float = LINEAR_TOLERANCE, angular: float = ANGULAR_TOLERANCE) -> Mesh:
    """A closed outward-facing mesh of ``shape``, with coincident vertices merged."""
    points, faces = _triangulate(shape, tolerance, angular)
    if not len(faces):
        raise ValueError("the shape produced no triangles")
    unique, inverse = np.unique(np.round(points, 5), axis=0, return_inverse=True)
    faces = np.asarray(inverse).reshape(-1)[faces]
    keep = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
    mesh = Mesh(unique, faces[keep])
    if mesh.signed_volume < 0:
        mesh = Mesh(mesh.vertices, mesh.faces[:, ::-1])
    return mesh


def _triangulate(shape, tolerance: float, angular: float) -> tuple[np.ndarray, np.ndarray]:
    """Every face's triangulation, read node by node.

    CadQuery's own ``tessellate`` walks each face's triangle array in a way that costs milliseconds per
    face, which is minutes for an imported mesh with thousands of faces.
    """
    shape.mesh(tolerance, angular)
    points: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    for face in shape.Faces():
        location = TopLoc_Location()
        poly = BRep_Tool.Triangulation_s(face.wrapped, location)
        if poly is None:
            continue
        transform, offset = location.Transformation(), len(points)
        for index in range(1, poly.NbNodes() + 1):
            node = poly.Node(index).Transformed(transform)
            points.append((node.X(), node.Y(), node.Z()))
        reverse = face.wrapped.Orientation() == TopAbs_REVERSED
        for index in range(1, poly.NbTriangles() + 1):
            a, b, c = poly.Triangle(index).Get()
            faces.append((offset + a - 1, offset + c - 1, offset + b - 1) if reverse
                         else (offset + a - 1, offset + b - 1, offset + c - 1))
    return np.array(points, dtype=float).reshape(-1, 3), np.array(faces, dtype=np.int64).reshape(-1, 3)
