"""Triangle meshes tessellated from CadQuery shapes."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

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
    raw_vertices, raw_faces = shape.tessellate(tolerance, angular)
    if not raw_faces:
        raise ValueError("the shape produced no triangles")
    points = np.array([vertex.toTuple() for vertex in raw_vertices], dtype=float)
    faces = np.array(raw_faces, dtype=np.int64)
    unique, inverse = np.unique(np.round(points, 5), axis=0, return_inverse=True)
    faces = np.asarray(inverse).reshape(-1)[faces]
    keep = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
    mesh = Mesh(unique, faces[keep])
    if mesh.signed_volume < 0:
        mesh = Mesh(mesh.vertices, mesh.faces[:, ::-1])
    return mesh
