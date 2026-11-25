"""Arrange parts on named plates for a single filament.

Big parts share plates with big parts and small with small. Each part is turned so its long side
runs along X, and rows fill along X before moving on in Y, so the layout stays short in Y: on a
bed-slinger the bed carries Y, and fewer long Y moves shake the print less. Each plate's layout is
then centred on the bed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_GAP = 5.0
DEFAULT_MARGIN = 5.0
#: A part joins a plate only if its footprint is at least this share of the plate's largest part.
DEFAULT_SIZE_RATIO = 0.25


class ArrangeError(ValueError):
    """A part cannot be placed on the chosen printer."""


@dataclass(frozen=True)
class Printer:
    name: str
    width: float
    depth: float
    height: float
    #: Bed areas the nozzle must not print on, as (x0, y0, x1, y1).
    exclude: tuple[tuple[float, float, float, float], ...] = ()
    #: BambuStudio's default process and filament presets for this printer.
    process: str = "0.20mm Standard @BBL X1C"
    filament: str = "Bambu PLA Basic @BBL X1C"


_CORNER = ((0.0, 0.0, 18.0, 28.0),)
#: Sizes and exclusion zones copied from BambuStudio's own machine profiles.
PRINTERS: dict[str, Printer] = {
    "x1c": Printer("Bambu Lab X1 Carbon", 256, 256, 250, _CORNER),
    "p1s": Printer("Bambu Lab P1S", 256, 256, 250, _CORNER, filament="Bambu PLA Basic @BBL P1S 0.4 nozzle"),
    "p1p": Printer("Bambu Lab P1P", 256, 256, 250, _CORNER, "0.20mm Standard @BBL P1P", "Bambu PLA Basic @BBL P1P"),
    "a1": Printer("Bambu Lab A1", 256, 256, 256, (), "0.20mm Standard @BBL A1", "Bambu PLA Basic @BBL A1"),
    "a1mini": Printer("Bambu Lab A1 mini", 180, 180, 180, (), "0.20mm Standard @BBL A1M", "Bambu PLA Basic @BBL A1M"),
}
DEFAULT_PRINTER = "x1c"


def get_printer(name: str) -> Printer:
    try:
        return PRINTERS[name.lower()]
    except KeyError:
        raise ValueError(f"unknown printer {name!r}; choose one of {sorted(PRINTERS)}") from None


@dataclass
class Item:
    """A part ready to place. ``width`` and ``depth`` are its footprint with the long side on X."""

    name: str
    width: float
    depth: float
    height: float
    payload: object = None

    @property
    def area(self) -> float:
        return self.width * self.depth


@dataclass
class Placement:
    item: Item
    x: float
    y: float

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.x + self.item.width, self.y + self.item.depth)


@dataclass
class Plate:
    index: int
    name: str = ""
    placements: list[Placement] = field(default_factory=list)


def _overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _check_size(item: Item, printer: Printer, margin: float) -> None:
    if (item.width > printer.width - 2 * margin or item.depth > printer.depth - 2 * margin
            or item.height > printer.height):
        raise ArrangeError(f"{item.name} is {item.width:.1f} x {item.depth:.1f} x {item.height:.1f} mm, which does not "
                           f"fit the {printer.name} bed ({printer.width:g} x {printer.depth:g} x {printer.height:g} mm "
                           f"with a {margin:g} mm margin)")


class _Cursor:
    """Fills one plate in rows along X."""

    def __init__(self, printer: Printer, gap: float, margin: float):
        self.printer, self.gap, self.margin = printer, gap, margin
        self.x, self.row_y, self.row_depth = margin, margin, 0.0

    def _clear_of_exclusions(self, item: Item) -> bool:
        """Slide right past any exclusion zone; False when that pushes the part off the row."""
        moved = True
        while moved:
            moved = False
            rect = (self.x, self.row_y, self.x + item.width, self.row_y + item.depth)
            for zone in self.printer.exclude:
                if _overlaps(rect, (zone[0] - self.gap, zone[1] - self.gap, zone[2] + self.gap, zone[3] + self.gap)):
                    self.x, moved = zone[2] + self.gap, True
        return self.x + item.width <= self.printer.width - self.margin

    def _new_row(self) -> None:
        self.row_y += self.row_depth + self.gap
        self.x, self.row_depth = self.margin, 0.0

    def _rise_past_exclusions(self) -> None:
        self.row_y = max(self.row_y, *(zone[3] + self.gap for zone in self.printer.exclude))
        self.x = self.margin

    def place(self, item: Item) -> Placement | None:
        """The next free spot for ``item``, or None when this plate is full."""
        if self.x > self.margin and self.x + item.width > self.printer.width - self.margin:
            self._new_row()
        if not self._clear_of_exclusions(item):
            if self.row_depth:
                self._new_row()
                if not self._clear_of_exclusions(item):
                    self._rise_past_exclusions()
            else:
                self._rise_past_exclusions()
        if self.row_y + item.depth > self.printer.depth - self.margin:
            return None
        placement = Placement(item, self.x, self.row_y)
        self.x += item.width + self.gap
        self.row_depth = max(self.row_depth, item.depth)
        return placement


def _fits(rects, printer: Printer, gap: float, margin: float) -> bool:
    for rect in rects:
        if (rect[0] < margin - 1e-9 or rect[1] < margin - 1e-9 or rect[2] > printer.width - margin + 1e-9
                or rect[3] > printer.depth - margin + 1e-9):
            return False
        for zone in printer.exclude:
            if _overlaps(rect, (zone[0] - gap, zone[1] - gap, zone[2] + gap, zone[3] + gap)):
                return False
    return True


def _centre(plate: Plate, printer: Printer, gap: float, margin: float) -> None:
    """Move the plate's parts to the middle of the bed: each row centred on X, the rows together on Y.

    If that lands on an exclusion zone, the packed layout moves as one block to the clear spot nearest
    the middle instead. A layout that fits nowhere else stays where it was packed.
    """
    rows: dict[float, list[Placement]] = {}
    for placement in plate.placements:
        rows.setdefault(placement.y, []).append(placement)
    y0 = min(p.y for p in plate.placements)
    y1 = max(p.y + p.item.depth for p in plate.placements)
    dy = (printer.depth - (y1 - y0)) / 2 - y0
    moved = {}
    for row in rows.values():
        x0, x1 = min(p.x for p in row), max(p.x + p.item.width for p in row)
        dx = (printer.width - (x1 - x0)) / 2 - x0
        for p in row:
            moved[id(p)] = (p.x + dx, p.y + dy)
    rects = [(x, y, x + p.item.width, y + p.item.depth) for p in plate.placements for x, y in [moved[id(p)]]]
    if _fits(rects, printer, gap, margin):
        for p in plate.placements:
            p.x, p.y = moved[id(p)]
        return
    # Rows centred on their own hit a zone: slide the packed block instead, nearest the middle first.
    x0 = min(p.x for p in plate.placements)
    x1 = max(p.x + p.item.width for p in plate.placements)
    lo_x, hi_x = margin - x0, printer.width - margin - x1
    lo_y, hi_y = margin - y0, printer.depth - margin - y1
    ideal = ((printer.width - (x1 - x0)) / 2 - x0, dy)
    steps = lambda lo, hi: [lo + (hi - lo) * i / max(1, round(hi - lo)) for i in range(max(1, round(hi - lo)) + 1)]
    shifts = sorted(((sx, sy) for sx in steps(lo_x, hi_x) for sy in steps(lo_y, hi_y)),
                    key=lambda s: (s[0] - ideal[0]) ** 2 + (s[1] - ideal[1]) ** 2)
    for sx, sy in shifts:
        if _fits([(r[0] + sx, r[1] + sy, r[2] + sx, r[3] + sy) for r in (p.rect for p in plate.placements)],
                 printer, gap, margin):
            for p in plate.placements:
                p.x, p.y = p.x + sx, p.y + sy
            return


def _size_label(plate: Plate, printer: Printer) -> str:
    longest = max(placement.item.width for placement in plate.placements) / printer.width
    return "big" if longest >= 0.5 else "medium" if longest >= 0.25 else "small"


def arrange(items: list[Item], printer: Printer, *, project: str = "plate", gap: float = DEFAULT_GAP,
            margin: float = DEFAULT_MARGIN, size_ratio: float = DEFAULT_SIZE_RATIO) -> list[Plate]:
    """Place every item, largest first, and name the resulting plates."""
    if not items:
        raise ArrangeError("there is nothing to arrange")
    if not 0 <= size_ratio <= 1:
        raise ValueError(f"size_ratio must be between 0 and 1, got {size_ratio}")
    for item in items:
        _check_size(item, printer, margin)
    plates: list[Plate] = []
    cursor = None
    anchor = 0.0
    for item in sorted(items, key=lambda it: (-it.area, -it.height, it.name)):
        placement = None
        if cursor is not None and item.area >= size_ratio * anchor:
            placement = cursor.place(item)
        if placement is None:
            plates.append(Plate(len(plates) + 1))
            cursor = _Cursor(printer, gap, margin)
            anchor = item.area
            placement = cursor.place(item)
            if placement is None:
                raise ArrangeError(f"{item.name} cannot be placed clear of the bed's exclusion zone on {printer.name}")
        plates[-1].placements.append(placement)
    for plate in plates:
        _centre(plate, printer, gap, margin)
        plate.name = f"{project} {plate.index} {_size_label(plate, printer)}"
    return plates
