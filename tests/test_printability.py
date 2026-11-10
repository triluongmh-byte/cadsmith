import math

import cadquery as cq
import numpy as np
import pytest

from cadsmith.mesh import tessellate
from cadsmith.printability import ORIENTATIONS, check_part, choose_orientation, find_thin_regions, overhang_by_orientation

from conftest import box


def by_down(results):
    return {result.down: result for result in results}


def test_orientations_put_the_named_face_on_the_bed():
    for name, rotation in ORIENTATIONS.items():
        axis = {"X": 0, "Y": 1, "Z": 2}[name[1]]
        face = np.zeros(3)
        face[axis] = 1 if name[0] == "+" else -1
        assert rotation @ face == pytest.approx([0, 0, -1])
        assert np.linalg.det(rotation) == pytest.approx(1)


def test_thick_box_has_no_thin_walls(open_box):
    report = check_part("box", open_box(2.0))
    assert report.passed
    assert report.thin_regions == []
    assert report.samples > 1000


def test_solid_cube_edges_are_not_mistaken_for_walls():
    assert find_thin_regions(tessellate(box(20, 20, 20)))[0] == []


def test_thin_shell_is_measured(open_box):
    report = check_part("box", open_box(0.5))
    assert not report.passed
    assert report.thin_regions[0].thickness == pytest.approx(0.5, abs=0.01)


def test_one_small_fin_is_found_and_located():
    part = cq.Workplane("XY").box(60, 40, 10).union(cq.Workplane("XY").box(0.6, 20, 15).translate((12, 5, 12))).val()
    report = check_part("fin", part)
    assert not report.passed
    region = report.thin_regions[0]
    assert region.thickness == pytest.approx(0.6, abs=0.01)
    assert region.location[0] == pytest.approx(12, abs=0.5)
    assert region.location[2] > 5
    assert region.area == pytest.approx(20 * 15, rel=0.1)


def test_curved_thin_wall_is_measured():
    tube = cq.Workplane("XY").circle(15).circle(14.5).extrude(20).val()
    regions, _ = find_thin_regions(tessellate(tube))
    assert regions[0].thickness == pytest.approx(0.5, abs=0.02)


def test_wall_exactly_at_the_limit_passes(open_box):
    report = check_part("box", open_box(0.8))
    assert not report.errors
    assert report.thin_regions[0].severity == "warning"


def test_between_minimum_and_warning_is_only_a_warning(open_box):
    report = check_part("box", open_box(1.0))
    assert report.passed
    assert report.warnings


def test_two_separate_thin_spots_are_two_regions():
    part = (cq.Workplane("XY").box(100, 40, 10)
            .union(cq.Workplane("XY").box(0.6, 20, 15).translate((-40, 0, 12)))
            .union(cq.Workplane("XY").box(0.6, 20, 15).translate((40, 0, 12))).val())
    regions, _ = find_thin_regions(tessellate(part))
    assert len([region for region in regions if region.severity == "error"]) == 2


def test_cube_has_no_overhang_in_any_orientation():
    results = overhang_by_orientation(tessellate(box(20, 20, 20)))
    assert [result.overhang_area for result in results] == [0] * 6
    assert [result.bed_contact for result in results] == [400] * 6


def t_shape():
    """A 10 x 10 x 20 stem under a 30 x 10 x 5 cap: the cap's underside overhangs."""
    return (cq.Workplane("XY").box(10, 10, 20, centered=(True, True, False))
            .union(cq.Workplane("XY").workplane(offset=20).box(30, 10, 5, centered=(True, True, False))).val())


def test_overhang_area_is_measured_per_orientation():
    results = by_down(overhang_by_orientation(tessellate(t_shape())))
    assert results["-Z"].overhang_area == pytest.approx(200)
    assert results["+Z"].overhang_area == pytest.approx(0)
    assert results["-Z"].bed_contact == pytest.approx(100)
    assert results["+Z"].bed_contact == pytest.approx(300)
    assert results["-Z"].height == pytest.approx(25)


def test_best_orientation_avoids_overhang():
    report = check_part("t", t_shape())
    assert report.chosen in ("+Z", "-Y", "+Y")
    assert not report.needs_support


def test_declared_orientation_is_respected_and_warned():
    report = check_part("t", t_shape(), down="-Z")
    assert report.chosen == "-Z"
    assert report.needs_support
    assert any("overhangs" in warning for warning in report.warnings)


def chamfered(angle_from_vertical):
    """A block with a downward slope facing -X at the given angle from vertical."""
    height, run = 20, 20 * math.tan(math.radians(angle_from_vertical))
    return (cq.Workplane("XZ").polyline([(0, 0), (30, 0), (30, height), (-run, height)]).close().extrude(-20).val())


def test_slope_at_the_limit_is_fine_and_past_it_is_overhang():
    at_limit = by_down(overhang_by_orientation(tessellate(chamfered(44.9))))["-Z"]
    past_limit = by_down(overhang_by_orientation(tessellate(chamfered(50))))["-Z"]
    assert at_limit.overhang_area == 0
    assert past_limit.overhang_area > 0


def test_limit_can_be_changed():
    relaxed = by_down(overhang_by_orientation(tessellate(chamfered(50)), overhang_deg=55))["-Z"]
    assert relaxed.overhang_area == 0


def test_too_big_for_bed():
    mesh = tessellate(box(300, 20, 20))
    results = by_down(overhang_by_orientation(mesh))
    assert not results["-Z"].fits_bed
    report = check_part("long", box(300, 20, 20))
    assert not report.passed
    assert choose_orientation(report.orientations) is None


def test_tall_part_lies_down_to_fit():
    report = check_part("tall", box(20, 20, 300), bed=(320, 320, 250))
    assert report.passed
    assert report.chosen in ("-X", "+X", "-Y", "+Y")


def test_a_point_on_the_bed_is_not_stable():
    cone = cq.Solid.makeCone(0.0, 10.0, 20.0)
    results = by_down(overhang_by_orientation(tessellate(cone)))
    assert not results["-Z"].stable
    assert results["+Z"].stable
    assert choose_orientation(list(results.values())).down == "+Z"


def test_loose_bodies_fail():
    parts = cq.Compound.makeCompound([box(10, 10, 10), box(10, 10, 10, at=(30, 0, 0))])
    report = check_part("two", parts)
    assert not report.passed
    assert any("separate bodies" in error for error in report.errors)


@pytest.mark.parametrize("kwargs", [{"min_wall": 0}, {"down": "up"}])
def test_invalid_arguments(kwargs):
    with pytest.raises(ValueError):
        check_part("x", box(10, 10, 10), **kwargs)
