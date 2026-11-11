"""Bring an STL mesh, such as a sculpted figure, into a model as a solid.

The mesh is thinned to ``detail`` triangles and sewn into a B-rep solid, so it can be moved, scaled,
unioned with a printed base or cut for a peg hole, and then checked like any other part. It cannot be
edited feature by feature: change it with booleans. The sewn solid is cached next to the STL, so later
builds of the same mesh are fast.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import cadquery as cq
import numpy as np
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeSolid,
                                BRepBuilderAPI_Sewing)
from OCP.BRepTools import BRepTools
from OCP.gp import gp_Pnt
from OCP.ShapeFix import ShapeFix_Solid
from OCP.TopAbs import TopAbs_SHELL
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shape

from cadsmith.stl import open_edges, read_stl

DEFAULT_DETAIL = 5000
MIN_DETAIL, MAX_DETAIL = 200, 30000
_CACHE_VERSION = "1"


def _resolve(path: str | Path, caller: Path | None) -> Path:
    path = Path(path)
    if path.is_absolute():
        return path
    for base in ([caller.parent] if caller else []) + [Path.cwd()]:
        if (base / path).is_file():
            return base / path
    raise FileNotFoundError(f"no STL at {path}; paths are relative to the model file")


def _thin(vertices: np.ndarray, faces: np.ndarray, detail: int) -> tuple[np.ndarray, np.ndarray]:
    if len(faces) <= detail:
        return vertices, faces
    import fast_simplification

    points, triangles = fast_simplification.simplify(vertices.astype(np.float32), faces.astype(np.int32),
                                                     target_count=detail)
    return np.asarray(points, dtype=float), np.asarray(triangles, dtype=np.int64)


def _sew(vertices: np.ndarray, faces: np.ndarray, name: str) -> TopoDS_Shape:
    sewing = BRepBuilderAPI_Sewing(1e-4)
    points = [gp_Pnt(*map(float, p)) for p in vertices]
    for a, b, c in faces:
        polygon = BRepBuilderAPI_MakePolygon(points[a], points[b], points[c], True)
        face = BRepBuilderAPI_MakeFace(polygon.Wire(), True)
        if face.IsDone():
            sewing.Add(face.Face())
    sewing.Perform()
    sewn = sewing.SewedShape()
    solids = []
    explorer = TopExp_Explorer(sewn, TopAbs_SHELL)
    while explorer.More():
        shell = TopoDS.Shell_s(explorer.Current())
        if not BRep_Tool.IsClosed_s(shell):
            raise ValueError(f"{name} is not a closed mesh, so it has no inside to print. Repair it first, "
                             "for example with Fix model in Bambu Studio, then import it again.")
        fix = ShapeFix_Solid(BRepBuilderAPI_MakeSolid(shell).Solid())
        fix.Perform()
        solids.append(cq.Solid(fix.Solid()))
        explorer.Next()
    if not solids:
        raise ValueError(f"{name} has no surface that could be sewn into a solid")
    shape = solids[0] if len(solids) == 1 else cq.Compound.makeCompound(solids)
    return shape.wrapped


def import_stl(path: str | Path, *, size: float | None = None, scale: float = 1.0, detail: int = DEFAULT_DETAIL,
               center: bool = True) -> cq.Workplane:
    """An STL mesh as a solid: ``size`` sets its longest side in mm, else ``scale`` multiplies it.

    ``path`` is relative to the model file, such as ``"uploads/figure.stl"``. ``center`` puts the mesh
    in the middle of X and Y with its lowest point at Z=0; move it with ``.translate()`` afterwards.
    ``detail`` is the triangle count it is thinned to (200 to 30000): more is smoother and slower.
    Example: ``figure = import_stl("uploads/cat.stl", size=60)``.
    """
    caller = sys._getframe(1).f_code.co_filename
    file = _resolve(path, Path(caller) if caller and not caller.startswith("<") else None)
    if not MIN_DETAIL <= detail <= MAX_DETAIL:
        raise ValueError(f"detail must be {MIN_DETAIL} to {MAX_DETAIL} triangles, got {detail}")
    if size is not None and size <= 0 or scale <= 0:
        raise ValueError("size and scale must be positive")
    data = file.read_bytes()
    key = hashlib.sha1(data + repr((_CACHE_VERSION, size, scale, detail, center)).encode()).hexdigest()[:16]
    cache = file.parent / ".cache" / f"{file.stem}-{key}.brep"
    shape = TopoDS_Shape()
    if cache.is_file() and BRepTools.Read_s(shape, str(cache), BRep_Builder()) and not shape.IsNull():
        return cq.Workplane("XY").newObject([cq.Shape.cast(shape)])

    vertices, faces = read_stl(file)
    low, high = vertices.min(axis=0), vertices.max(axis=0)
    factor = size / float((high - low).max()) if size is not None else scale
    vertices = vertices * factor
    if center:
        low, high = low * factor, high * factor
        vertices = vertices - np.array([(low[0] + high[0]) / 2, (low[1] + high[1]) / 2, low[2]])
    if open_edges(faces):
        raise ValueError(f"{file.name} has holes in its surface ({open_edges(faces)} open edges), so it has no "
                         "inside to print. Repair it first, for example with Fix model in Bambu Studio.")
    vertices, faces = _thin(vertices, faces, detail)
    solid = _sew(vertices, faces, file.name)
    cache.parent.mkdir(exist_ok=True)
    BRepTools.Write_s(solid, str(cache))
    return cq.Workplane("XY").newObject([cq.Shape.cast(solid)])
