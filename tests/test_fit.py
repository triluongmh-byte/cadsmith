import cadquery as cq
import pytest

from cadsmith import Fit, Model, Part
from cadsmith.fit import check_fits

from conftest import box


def pair(a, b, *fits):
    return Model("m", [Part("a", a), Part("b", b)], list(fits))


def lidded(side_gap, *fits, wall=2.0):
    """A box with a plate resting on its rim and a lip hanging into the cavity."""
    base = cq.Workplane("XY").box(50, 50, 20, centered=(True, True, False)).faces(">Z").shell(-wall).val()
    cavity = 50 - 2 * wall
    lip_outer = cavity - 2 * side_gap
    lid = (cq.Workplane("XY").workplane(offset=20).box(50, 50, 2, centered=(True, True, False))
           .union(cq.Workplane("XY").workplane(offset=15).rect(lip_outer, lip_outer).rect(lip_outer - 3, lip_outer - 3).extrude(5))).val()
    return pair(base, lid, *fits)


def test_gap_at_the_default_passes():
    result = check_fits(pair(box(10, 10, 10), box(10, 10, 10, at=(10.3, 0, 0))))[0]
    assert result.passed
    assert result.measured == pytest.approx(0.3, abs=1e-3)
    assert result.required == 0.3


def test_gap_below_the_default_fails():
    result = check_fits(pair(box(10, 10, 10), box(10, 10, 10, at=(10.2, 0, 0))))[0]
    assert not result.passed
    assert result.measured == pytest.approx(0.2, abs=1e-3)
    assert result.location[0] == pytest.approx(5.1, abs=0.01)


def test_each_pair_can_declare_its_own_clearance():
    tight = pair(box(10, 10, 10), box(10, 10, 10, at=(10.2, 0, 0)), Fit("a", "b", 0.1))
    loose = pair(box(10, 10, 10), box(10, 10, 10, at=(10.4, 0, 0)), Fit("a", "b", 0.5))
    assert check_fits(tight)[0].passed
    assert not check_fits(loose)[0].passed


def test_default_clearance_can_be_changed():
    model = pair(box(10, 10, 10), box(10, 10, 10, at=(10.2, 0, 0)))
    assert check_fits(model, default_clearance=0.15)[0].passed


def test_overlap_fails_and_reports_volume():
    result = check_fits(pair(box(10, 10, 10), box(10, 10, 10, at=(9, 0, 0))))[0]
    assert not result.passed
    assert result.overlap_mm3 == pytest.approx(100, rel=0.01)


def test_one_part_inside_another_is_caught():
    assert not check_fits(pair(box(20, 20, 20), box(2, 2, 2)))[0].passed


def test_every_pair_is_checked_without_declaring_it():
    model = Model("m", [Part("a", box(10, 10, 10)), Part("b", box(10, 10, 10, at=(30, 0, 0))),
                        Part("c", box(10, 10, 10, at=(40.1, 0, 0)))])
    results = {(r.a, r.b): r.passed for r in check_fits(model)}
    assert results == {("a", "b"): True, ("a", "c"): True, ("b", "c"): False}


def test_lid_resting_on_its_rim_fails_without_a_seat():
    assert not check_fits(lidded(0.3))[0].passed


def test_seated_lid_with_clearance_passes():
    result = check_fits(lidded(0.3, Fit("a", "b", 0.3, seat="z")))[0]
    assert result.passed
    assert result.measured == pytest.approx(0.3, abs=1e-3)


def test_seated_lid_with_tight_side_gap_fails():
    result = check_fits(lidded(0.1, Fit("a", "b", 0.3, seat="z")))[0]
    assert not result.passed
    assert result.measured == pytest.approx(0.1, abs=1e-3)


def test_seat_does_not_excuse_a_lip_built_at_cavity_size():
    assert not check_fits(lidded(0.0, Fit("a", "b", 0.3, seat="z")))[0].passed


def test_seat_on_the_wrong_axis_does_not_excuse_contact():
    assert not check_fits(lidded(0.3, Fit("a", "b", 0.3, seat="x")))[0].passed


def test_seat_does_not_excuse_overlap():
    base = box(10, 10, 10)
    assert not check_fits(pair(base, box(10, 10, 10, at=(0, 0, 9)), Fit("a", "b", 0.3, seat="z")))[0].passed


def test_plain_stack_on_a_seat_passes():
    result = check_fits(pair(box(10, 10, 10), box(10, 10, 10, at=(0, 0, 10)), Fit("a", "b", 0.3, seat="z")))[0]
    assert result.passed
    assert result.measured is None
