import pytest

from cadsmith.plates import ArrangeError, Item, Printer, arrange, get_printer

OPEN = Printer("open", 200, 200, 200)


def names(plate):
    return [placement.item.name for placement in plate.placements]


def rects_clear(plate, gap):
    rects = [placement.rect for placement in plate.placements]
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            apart = a[2] + gap <= b[0] + 1e-9 or b[2] + gap <= a[0] + 1e-9 or a[3] + gap <= b[1] + 1e-9 or b[3] + gap <= a[1] + 1e-9
            if not apart:
                return False
    return True


def test_fills_along_x_first():
    plate = arrange([Item(f"p{i}", 40, 30, 10) for i in range(3)], OPEN)[0]
    assert len({placement.y for placement in plate.placements}) == 1
    assert [placement.x for placement in plate.placements] == [5, 50, 95]


def test_new_row_only_when_x_is_full():
    plate = arrange([Item(f"p{i}", 60, 30, 10) for i in range(4)], OPEN)[0]
    ys = [placement.y for placement in plate.placements]
    assert ys[0] == ys[1] == ys[2]
    assert ys[3] == ys[0] + 30 + 5


def test_largest_goes_first():
    plate = arrange([Item("small", 40, 40, 5), Item("big", 60, 60, 5), Item("mid", 50, 50, 5)], OPEN)[0]
    assert names(plate) == ["big", "mid", "small"]


def test_small_parts_do_not_share_a_plate_with_big_ones():
    plates = arrange([Item("big", 150, 150, 5), Item("tiny", 20, 20, 5)], OPEN)
    assert [names(plate) for plate in plates] == [["big"], ["tiny"]]


def test_size_ratio_can_allow_mixing():
    assert len(arrange([Item("big", 150, 30, 5), Item("tiny", 20, 20, 5)], OPEN, size_ratio=0.0)) == 1


def test_overflow_starts_a_new_plate():
    plates = arrange([Item(f"p{i}", 120, 120, 5) for i in range(3)], OPEN)
    assert [len(plate.placements) for plate in plates] == [1, 1, 1]


def test_every_part_is_placed_once_inside_the_bed_with_a_gap():
    items = [Item(f"p{i}", 20 + 3 * i, 15 + i, 5) for i in range(30)]
    printer = get_printer("x1c")
    plates = arrange(items, printer, size_ratio=0.0)
    placed = [name for plate in plates for name in names(plate)]
    assert sorted(placed) == sorted(item.name for item in items)
    for plate in plates:
        assert rects_clear(plate, 5.0)
        for placement in plate.placements:
            x0, y0, x1, y1 = placement.rect
            assert x0 >= 5 and y0 >= 5 and x1 <= printer.width - 5 and y1 <= printer.depth - 5


def test_exclusion_zone_is_avoided():
    printer = get_printer("x1c")
    placement = arrange([Item("p", 30, 30, 5)], printer)[0].placements[0]
    x0, y0, x1, y1 = placement.rect
    assert x0 >= 18 + 5 or y0 >= 28 + 5


def test_full_width_part_rises_past_the_exclusion_zone():
    printer = get_printer("x1c")
    placement = arrange([Item("wide", 240, 40, 5)], printer)[0].placements[0]
    assert placement.x == 5
    assert placement.y >= 33


def test_row_after_the_zone_starts_at_the_margin():
    printer = get_printer("x1c")
    plate = arrange([Item(f"p{i}", 100, 100, 5) for i in range(3)], printer)[0]
    assert plate.placements[2].x == 5
    assert plate.placements[2].y > 100


def test_plates_are_named_by_project_and_size():
    plates = arrange([Item("big", 150, 150, 5), Item("tiny", 20, 20, 5)], OPEN, project="desk")
    assert [plate.name for plate in plates] == ["desk 1 big", "desk 2 small"]


@pytest.mark.parametrize("item", [Item("wide", 300, 20, 5), Item("deep", 150, 196, 5), Item("tall", 20, 20, 300)])
def test_too_big_is_refused_by_name(item):
    with pytest.raises(ArrangeError, match=item.name):
        arrange([item], OPEN)


def test_nothing_to_arrange():
    with pytest.raises(ArrangeError):
        arrange([], OPEN)


def test_unknown_printer():
    with pytest.raises(ValueError, match="unknown printer"):
        get_printer("ender3")


def test_bad_size_ratio():
    with pytest.raises(ValueError):
        arrange([Item("p", 10, 10, 10)], OPEN, size_ratio=2)
