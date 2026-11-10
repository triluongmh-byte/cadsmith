import cadquery as cq
import pytest

from cadsmith.cadlib.enclosure import hollow_box
from cadsmith.cadlib.fits import mating_clearance, peg_for, print_in_place_gap, slot_for
from cadsmith.cadlib.layout import circle_points, four_corner_points, grid_points
from cadsmith.cadlib.tables import SCREW_TABLE


def test_fit_helpers_share_one_nominal():
    assert slot_for(10, "slip") == pytest.approx(10.4)
    assert peg_for(10.4, "slip") == pytest.approx(10)
    assert mating_clearance("press") < 0
    with pytest.raises(ValueError):
        mating_clearance("nope")


def test_print_in_place_needs_more_gap_in_z():
    gap = print_in_place_gap()
    assert gap["z"] > gap["xy"]
    assert print_in_place_gap(material="PETG")["xy"] > gap["xy"]


def test_layout_points():
    assert four_corner_points(length=80, width=60, margin=5)[0] == (35.0, 25.0)
    assert len(grid_points(n_x=3, n_y=2, pitch_x=10)) == 6
    assert len(circle_points(n=6, radius=5)) == 6
    with pytest.raises(ValueError):
        four_corner_points(length=8, width=6, margin=5)


def test_hollow_box_has_the_requested_wall():
    body = hollow_box(length=40, width=30, height=20, wall=2)
    assert isinstance(body, cq.Workplane)
    assert body.val().Volume() == pytest.approx(40 * 30 * 20 - 36 * 26 * 18)
    with pytest.raises(ValueError):
        hollow_box(length=4, width=4, height=4, wall=3)


def test_tables_have_common_sizes():
    assert SCREW_TABLE["M3"]["clearance"] > 3
