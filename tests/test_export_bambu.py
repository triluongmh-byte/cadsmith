import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import cadquery as cq
import numpy as np
import pytest

from cadsmith import Model, Part
from cadsmith.bambu3mf import make_project, plate_origin, prepare
from cadsmith.export import export_part
from cadsmith.mesh import tessellate
from cadsmith.plates import get_printer

from conftest import box

CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
BAMBU = Path("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio")
X1C = get_printer("x1c")


def test_tessellation_is_closed_and_outward():
    mesh = tessellate(cq.Workplane("XY").box(10, 20, 30).faces(">Z").hole(4).val())
    assert mesh.signed_volume > 0
    edges = np.sort(mesh.faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    assert set(counts) == {2}


def test_export_writes_all_three_formats(tmp_path):
    paths = export_part(box(10, 20, 30), "block", tmp_path)
    assert sorted(path.name for path in paths.values()) == ["block.3mf", "block.step", "block.stl"]
    assert cq.importers.importStep(str(paths["step"])).val().Volume() == pytest.approx(6000)
    assert paths["stl"].stat().st_size > 84
    with zipfile.ZipFile(paths["3mf"]) as package:
        root = ET.fromstring(package.read("3D/3dmodel.model"))
    vertices = [[float(v.get(axis)) for axis in "xyz"] for v in root.iter(f"{CORE}vertex")]
    span = np.ptp(np.array(vertices), axis=0)
    assert span == pytest.approx([10, 20, 30], abs=1e-3)


def test_prepare_rests_on_the_bed_with_the_long_side_on_x():
    model = Model("m", [Part("rod", box(10, 60, 20, at=(100, 100, 100)), down="-Z")])
    item = prepare(model, X1C)[0]
    low, high = item.payload.mesh.bounds
    assert low[2] == pytest.approx(0)
    assert high - low == pytest.approx([60, 10, 20], abs=1e-6)
    assert (low[0], low[1]) == pytest.approx((-30, -5), abs=1e-6)
    assert (item.width, item.depth, item.height) == pytest.approx((60, 10, 20))


def test_prepare_applies_the_chosen_face_down():
    model = Model("m", [Part("p", box(10, 30, 40), down="-X")])
    item = prepare(model, X1C)[0]
    assert item.height == pytest.approx(10)


def test_prepare_makes_one_item_per_copy():
    items = prepare(Model("m", [Part("clip", box(10, 10, 10), quantity=3)]), X1C)
    assert [item.name for item in items] == ["clip_1", "clip_2", "clip_3"]


@pytest.mark.parametrize("index, count, expected", [
    (1, 1, (0, 0)), (2, 2, (307.2, 0)), (3, 3, (0, -307.2)), (4, 4, (307.2, -307.2)), (3, 5, (614.4, 0)),
])
def test_plate_origin_matches_bambu_layout(index, count, expected):
    assert plate_origin(index, count, X1C) == pytest.approx(expected)


def project(tmp_path, count=2, printer=X1C):
    parts = [Part(f"big{i}", box(150, 150, 20)) for i in range(count)] + [Part("small", box(20, 20, 20))]
    path = tmp_path / "project.3mf"
    return path, make_project([Model("desk", parts)], printer, path)


def test_project_records_named_plates_and_instances(tmp_path):
    path, plates = project(tmp_path)
    with zipfile.ZipFile(path) as package:
        settings = ET.fromstring(package.read("Metadata/model_settings.config"))
    found = settings.findall("plate")
    assert len(found) == len(plates) == 3
    values = [{m.get("key"): m.get("value") for m in plate.findall("metadata")} for plate in found]
    assert [value["plater_name"] for value in values] == ["desk 1 big", "desk 2 big", "desk 3 small"]
    assert [value["plater_id"] for value in values] == ["1", "2", "3"]
    assert all(len(plate.findall("model_instance")) == 1 for plate in found)
    assert {m.get("value") for obj in settings.findall("object") for m in obj.findall("metadata") if m.get("key") == "extruder"} == {"1"}


def test_project_looks_like_a_bambu_file_and_references_resolve(tmp_path):
    path, _ = project(tmp_path)
    with zipfile.ZipFile(path) as package:
        names = set(package.namelist())
        root_text = package.read("3D/3dmodel.model").decode()
        root = ET.fromstring(root_text)
        rels = package.read("3D/_rels/3dmodel.model.rels").decode()
    assert "BambuStudio-" in root_text
    assert "Metadata/project_settings.config" in names
    objects = re.findall(r'p:path="/([^"]+)"', root_text)
    assert objects and all(item in names and f"/{item}" in rels for item in objects)
    ids = {obj.get("id") for obj in root.iter(f"{CORE}object")}
    assert {item.get("objectid") for item in root.iter(f"{CORE}item")} == ids


def test_items_land_inside_their_own_plate(tmp_path):
    path, plates = project(tmp_path)
    with zipfile.ZipFile(path) as package:
        root = ET.fromstring(package.read("3D/3dmodel.model"))
    xy = [tuple(map(float, item.get("transform").split()[-3:-1])) for item in root.iter(f"{CORE}item")]
    placed = [(plate, placement) for plate in plates for placement in plate.placements]
    assert len(xy) == len(placed)
    for (x, y), (plate, placement) in zip(xy, placed):
        ox, oy = plate_origin(plate.index, len(plates), X1C)
        half_w, half_d = placement.item.width / 2, placement.item.depth / 2
        assert ox <= x - half_w and x + half_w <= ox + X1C.width
        assert oy <= y - half_d and y + half_d <= oy + X1C.depth


def test_project_settings_name_the_printer(tmp_path):
    small = [Part("p", box(50, 50, 10))]
    target = tmp_path / "mini.3mf"
    make_project([Model("m", small)], get_printer("a1mini"), target)
    with zipfile.ZipFile(target) as package:
        settings = json.loads(package.read("Metadata/project_settings.config"))
    assert settings["printable_area"][2] == "180x180"
    assert settings["printer_model"] == "Bambu Lab A1 mini"
    assert len(settings["filament_settings_id"]) == 1


@pytest.mark.slicer
@pytest.mark.skipif(not BAMBU.exists(), reason="BambuStudio is not installed")
def test_bambu_studio_slices_every_plate(tmp_path):
    path, plates = project(tmp_path)
    out = tmp_path / "sliced"
    out.mkdir()
    done = subprocess.run([str(BAMBU), "--debug", "1", "--slice", "0", "--outputdir", str(out), str(path)],
                          capture_output=True, text=True, timeout=240)
    result = json.loads((out / "result.json").read_text())
    assert done.returncode == 0, result.get("error_string")
    assert sorted(p.name for p in out.glob("*.gcode")) == [f"plate_{plate.index}.gcode" for plate in plates]
    shutil.rmtree(out)
