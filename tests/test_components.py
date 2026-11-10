import cadquery as cq
import pytest

from cadsmith.cadlib.components import (BOARD_TABLE, CELL_18650, SERVO_TABLE, board, board_hole_points, cell_18650, nut,
                                        screw, servo, servo_cutout, servo_hole_points, servo_horn)
from cadsmith.fit import check_fits
from cadsmith.loader import ModelError, load_model
from cadsmith.model import Fit, Model, Part


def size(shape):
    box = shape.val().BoundingBox()
    return box.xlen, box.ylen, box.zlen


@pytest.mark.parametrize("kind", sorted(SERVO_TABLE))
def test_servo_matches_its_table(kind):
    spec = SERVO_TABLE[kind]
    x, y, z = size(servo(kind))
    assert x == pytest.approx(spec["flange_length"], abs=0.01)
    assert y == pytest.approx(spec["width"], abs=0.01)
    assert z == pytest.approx(spec["height"] + spec["boss_h"] + spec["shaft_h"], abs=0.01)


@pytest.mark.parametrize("kind", sorted(SERVO_TABLE))
def test_servo_seats_in_its_cutout_without_overlap(kind):
    plate = cq.Workplane("XY").workplane(offset=-3).box(90, 50, 3, centered=(True, True, False))
    model = Model("m", [Part("plate", plate.cut(servo_cutout(kind)).val()), Part("servo", servo(kind).val(), bought=True)])
    assert all(result.passed for result in check_fits(model))


def test_a_servo_without_a_cutout_collides():
    plate = cq.Workplane("XY").workplane(offset=-3).box(90, 50, 3, centered=(True, True, False))
    model = Model("m", [Part("plate", plate.val()), Part("servo", servo().val(), bought=True)])
    assert not check_fits(model)[0].passed


def test_horn_reaches_along_x():
    box = servo_horn(arm=16).val().BoundingBox()
    assert box.xmax == pytest.approx(16, abs=0.01) and box.zmin == pytest.approx(0, abs=0.01)


def test_servo_holes_are_on_the_flange():
    spec = SERVO_TABLE["SG90"]
    assert [x + spec["shaft_offset"] for x, _ in servo_hole_points()] == pytest.approx([-13.85, 13.85])


@pytest.mark.parametrize("kind", sorted(BOARD_TABLE))
def test_board_footprint(kind):
    x, y, _ = size(board(kind))
    assert (x, y) == pytest.approx((BOARD_TABLE[kind]["length"], BOARD_TABLE[kind]["width"]), abs=0.01)


def test_board_without_a_pattern_has_no_holes():
    assert board_hole_points("esp32_devkit") == []
    assert len(board_hole_points("pi_pico")) == 4


def test_cell_is_along_x():
    assert size(cell_18650())[0] == pytest.approx(CELL_18650["length"], abs=0.01)


def test_screw_length_is_under_the_head():
    box = screw("M3", 12).val().BoundingBox()
    assert box.zmin == pytest.approx(-12, abs=0.01) and box.zmax == pytest.approx(3, abs=0.01)


def test_nut_has_a_threaded_bore():
    plain = cq.Workplane("XY").polygon(6, 6.35).extrude(2.4).val().Volume()
    assert nut("M3").val().Volume() < plain


@pytest.mark.parametrize("call", [lambda: servo("nope"), lambda: board("nope"), lambda: screw("M99"), lambda: nut("M99")])
def test_unknown_names_are_refused(call):
    with pytest.raises(ValueError, match="unknown"):
        call()


def box(offset=0):
    return cq.Workplane("XY").center(offset, 0).box(10, 10, 10).val()


def test_a_bought_part_may_touch_a_printed_one():
    model = Model("m", [Part("a", box()), Part("b", box(10), bought=True)])
    assert check_fits(model)[0].passed


def test_two_printed_parts_may_not_touch():
    assert not check_fits(Model("m", [Part("a", box()), Part("b", box(10))]))[0].passed


def test_two_bought_parts_are_skipped_unless_declared():
    parts = [Part("a", box(), bought=True), Part("b", box(5), bought=True), Part("c", box(100))]
    names = [{r.a, r.b} for r in check_fits(Model("m", parts))]
    assert {"a", "b"} not in names
    declared = check_fits(Model("m", parts, fits=[Fit("a", "b", min_clearance=0.1)]))
    assert not next(r for r in declared if {r.a, r.b} == {"a", "b"}).passed


def test_far_apart_parts_skip_the_measurement(monkeypatch):
    import cadsmith.fit as fit_module
    monkeypatch.setattr(fit_module, "_overlap", lambda *a: pytest.fail("measured a pair that cannot touch"))
    assert check_fits(Model("m", [Part("a", box()), Part("b", box(100))]))[0].passed


def write(tmp_path, body):
    path = tmp_path / "model.py"
    path.write_text("import cadquery as cq\n" + body)
    return path


TWO = 'def parts():\n    return {"a": cq.Workplane("XY").box(5, 5, 5), "b": cq.Workplane("XY").center(30, 0).box(5, 5, 5)}\n'


def test_loader_reads_bought(tmp_path):
    model = load_model(write(tmp_path, TWO + 'BOUGHT = {"b"}\n'))
    assert [part.bought for part in model.parts] == [False, True]
    assert [part.name for part in model.printed] == ["a"]


def test_loader_refuses_an_unknown_bought_name(tmp_path):
    with pytest.raises(ModelError, match="BOUGHT"):
        load_model(write(tmp_path, TWO + 'BOUGHT = {"zzz"}\n'))


def test_loader_refuses_a_model_with_nothing_to_print(tmp_path):
    with pytest.raises(ModelError, match="nothing to print"):
        load_model(write(tmp_path, TWO + 'BOUGHT = {"a", "b"}\n'))
