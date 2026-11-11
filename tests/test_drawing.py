import xml.etree.ElementTree as ET

import cadquery as cq
import pytest

from cadsmith.drawing import (SCALES, assembly_sheet, number, part_sheet, parts_list, pick_scale, project,
                              scale_label, stem)
from cadsmith.model import Model, Part

SVG = "{http://www.w3.org/2000/svg}"


def plate_with_holes():
    return (cq.Workplane("XY").box(60, 40, 5)
            .faces(">Z").workplane().rarray(40, 20, 2, 2).hole(3.4).val())


def texts(svg: str) -> list[str]:
    return [node.text for node in ET.fromstring(svg).iter(f"{SVG}text")]


def test_number_drops_trailing_zeros():
    assert number(60.0) == "60" and number(3.4) == "3.4" and number(12.345) == "12.3" and number(-0.01) == "0"


def test_scale_label_and_pick():
    assert scale_label(1) == "1:1" and scale_label(0.5) == "1:2" and scale_label(2) == "2:1"
    assert pick_scale(100, 50, 250, 250) == 2.0
    assert pick_scale(400, 100, 250, 250) == 0.5
    assert pick_scale(1e6, 1, 10, 10) == SCALES[-1]


def test_front_view_spans_x_and_z():
    view = project(cq.Workplane("XY").box(30, 20, 10).val(), "front")
    u0, v0, u1, v1 = view.bounds()
    assert u1 - u0 == pytest.approx(30, abs=0.01) and v1 - v0 == pytest.approx(10, abs=0.01)
    assert not view.hidden


def test_right_view_spans_y_and_z():
    u0, v0, u1, v1 = project(cq.Workplane("XY").box(30, 20, 10).val(), "right").bounds()
    assert u1 - u0 == pytest.approx(20, abs=0.01) and v1 - v0 == pytest.approx(10, abs=0.01)


def test_holes_show_as_circles_from_above_and_hidden_from_the_front():
    shape = plate_with_holes()
    top = project(shape, "top")
    assert sum(1 for _, _, r in top.circles if r == pytest.approx(1.7, abs=0.01)) >= 4
    assert project(shape, "front").hidden


def test_part_sheet_has_dimensions_and_hole_note():
    svg = part_sheet(Part("bracket", plate_with_holes(), quantity=2), "demo", "v3", "2/3")
    labels = texts(svg)
    assert {"60", "40", "5"} <= set(labels)
    assert "4× ⌀3.4" in labels
    assert "bracket ×2" in labels and "v3" in labels and "2/3" in labels and "1:1" in labels


def test_assembly_sheet_lists_parts_grouped():
    legs = {f"leg_{side}": cq.Workplane("XY").box(5, 5, 30).translate((x, 0, 0)).val()
            for side, x in (("left", -20), ("right", 20))}
    parts = [Part("body", cq.Workplane("XY").box(50, 20, 10).val()),
             *(Part(name, shape) for name, shape in legs.items()),
             Part("motor", cq.Workplane("XY").box(10, 10, 10).val(), bought=True)]
    rows = parts_list(parts)
    assert [(r.name, r.quantity, r.bought) for r in rows] == [("body", 1, False), ("leg", 2, False), ("motor", 1, True)]
    labels = texts(assembly_sheet(Model("robot", parts), "robot <v>", "v1"))
    assert "robot <v>" in labels and "leg" in labels and "motor (bought)" in labels


def test_stem_strips_side_words():
    assert stem("servo_hip_front_left") == "servo hip"
    assert stem("left") == "left"
