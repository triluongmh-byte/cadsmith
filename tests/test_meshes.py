import struct
from pathlib import Path

import cadquery as cq
import numpy as np
import pytest

from cadsmith.cadlib.meshes import import_stl
from cadsmith.drawing import HEAVY_FACES, part_sheet, project
from cadsmith.mesh import tessellate
from cadsmith.model import Part
from cadsmith.stl import StlError, describe_stl, open_edges, parse_stl


def blob():
    return cq.Workplane().sphere(10).union(cq.Workplane().sphere(6).translate((0, 0, 13)))


@pytest.fixture
def blob_stl(tmp_path):
    path = tmp_path / "blob.stl"
    blob().val().exportStl(str(path), 0.02, 0.1)
    return path


def ascii_tetra() -> bytes:
    corners = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (0, 0, 10)]
    faces = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]
    lines = ["solid t"]
    for face in faces:
        lines += ["facet normal 0 0 0", "outer loop", *(f"vertex {x} {y} {z}" for x, y, z in (corners[i] for i in face)),
                  "endloop", "endfacet"]
    return ("\n".join(lines + ["endsolid t"])).encode()


def test_parse_binary_and_ascii(blob_stl):
    vertices, faces = parse_stl(blob_stl.read_bytes())
    assert len(faces) > 500 and open_edges(faces) == 0
    vertices, faces = parse_stl(ascii_tetra())
    assert len(vertices) == 4 and len(faces) == 4
    info = describe_stl(ascii_tetra()).to_dict()
    assert info == {"triangles": 4, "size": [10, 10, 10], "open_edges": 0}


def test_parse_rejects_junk():
    with pytest.raises(StlError):
        parse_stl(b"hello there, not a mesh")
    with pytest.raises(StlError):
        parse_stl(struct.pack("<80sI", b"", 0))


def test_import_stl_sizes_centres_and_caches(blob_stl):
    first = import_stl(blob_stl, size=50).val()
    box = first.BoundingBox()
    assert max(box.xlen, box.ylen, box.zlen) == pytest.approx(50, abs=0.2)
    assert box.zmin == pytest.approx(0, abs=1e-6) and box.center.x == pytest.approx(0, abs=0.1)
    assert first.isValid() and first.Volume() > 0
    assert list((blob_stl.parent / ".cache").glob("blob-*.brep"))
    again = import_stl(blob_stl, size=50).val()
    assert again.Volume() == pytest.approx(first.Volume(), rel=1e-6)


def test_import_stl_thins_and_takes_booleans(blob_stl):
    figure = import_stl(blob_stl, detail=800)
    assert len(figure.val().Faces()) <= 800
    pegged = figure.cut(cq.Workplane().cylinder(6, 2).translate((0, 0, 1)))
    assert pegged.val().isValid() and pegged.val().Volume() < figure.val().Volume()


def test_import_stl_rejects_an_open_mesh(tmp_path):
    vertices = np.array([(0, 0, 0), (10, 0, 0), (0, 10, 0)], dtype="<f4")
    record = struct.pack("<12f2x", 0, 0, 1, *vertices.ravel())
    path = tmp_path / "open.stl"
    path.write_bytes(struct.pack("<80sI", b"", 1) + record)
    with pytest.raises(ValueError, match="open edges"):
        import_stl(path)


def test_import_stl_paths_are_relative_to_the_model_file(tmp_path, blob_stl):
    (tmp_path / "model.py").write_text(
        "from cadsmith.cadlib.meshes import import_stl\nresult = import_stl('blob.stl', size=20)\n")
    from cadsmith.loader import load_model
    model = load_model(tmp_path / "model.py")
    assert max(model.parts[0].shape.BoundingBox().zlen, 0) <= 20.01


def test_import_stl_bad_arguments(blob_stl):
    with pytest.raises(ValueError):
        import_stl(blob_stl, detail=10)
    with pytest.raises(FileNotFoundError):
        import_stl("missing.stl")


def test_tessellate_matches_volume_for_many_faces(blob_stl):
    shape = import_stl(blob_stl, detail=3000).val()
    mesh = tessellate(shape)
    assert mesh.signed_volume == pytest.approx(shape.Volume(), rel=1e-3)


def test_heavy_shapes_are_drawn_from_triangles(blob_stl):
    shape = import_stl(blob_stl, detail=HEAVY_FACES + 500).val()
    view = project(shape, "front")
    u0, v0, u1, v1 = view.bounds()
    box = shape.BoundingBox()
    assert u1 - u0 == pytest.approx(box.xlen, abs=0.5) and v1 - v0 == pytest.approx(box.zlen, abs=0.5)
    assert not view.hidden and len(view.visible) > 20
    assert part_sheet(Part("blob", shape), "demo").startswith("<svg")
