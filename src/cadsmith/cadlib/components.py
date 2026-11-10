"""Things you buy rather than print, as solids to place in an assembly.

List their names in the model's ``BOUGHT`` so they are shown and fit-checked but never printed.
Dimensions are nominal, from common datasheets, and vary between makers: measure yours before
printing a tight fit. All values are in millimetres.
"""

from __future__ import annotations

import math

import cadquery as cq

from cadsmith.cadlib.tables import NUT_TABLE, SCREW_TABLE

SERVO_TABLE: dict[str, dict] = {
    "SG90": {"length": 22.8, "width": 12.2, "height": 22.7, "flange_length": 32.2, "flange_thick": 2.5,
             "flange_z": 15.9, "holes": ((-13.85, 0.0), (13.85, 0.0)), "hole_d": 2.2, "shaft_offset": 5.5,
             "boss_d": 11.8, "boss_h": 4.0, "shaft_d": 4.8, "shaft_h": 3.2},
    "MG90S": {"length": 22.8, "width": 12.2, "height": 22.5, "flange_length": 32.2, "flange_thick": 2.5,
              "flange_z": 16.0, "holes": ((-13.85, 0.0), (13.85, 0.0)), "hole_d": 2.2, "shaft_offset": 5.5,
              "boss_d": 11.8, "boss_h": 4.0, "shaft_d": 4.8, "shaft_h": 3.2},
    "MG996R": {"length": 40.7, "width": 19.7, "height": 37.0, "flange_length": 54.5, "flange_thick": 2.5,
               "flange_z": 26.7, "holes": ((-24.75, -5.0), (-24.75, 5.0), (24.75, -5.0), (24.75, 5.0)), "hole_d": 4.5,
               "shaft_offset": 10.0, "boss_d": 14.0, "boss_h": 2.0, "shaft_d": 5.8, "shaft_h": 3.9},
}

BOARD_TABLE: dict[str, dict] = {
    "pi_pico": {"length": 51.0, "width": 21.0, "thick": 1.0, "parts_h": 3.0, "holes": (47.0, 11.4), "hole_d": 2.1},
    "pi_zero": {"length": 65.0, "width": 30.0, "thick": 1.4, "parts_h": 3.6, "holes": (58.0, 23.0), "hole_d": 2.75},
    "arduino_nano": {"length": 43.2, "width": 17.8, "thick": 1.6, "parts_h": 4.0, "holes": (40.64, 15.24), "hole_d": 1.7},
    "pca9685": {"length": 62.5, "width": 25.4, "thick": 1.6, "parts_h": 10.0, "holes": (55.9, 19.0), "hole_d": 2.5},
    "esp32_devkit": {"length": 51.5, "width": 28.3, "thick": 1.6, "parts_h": 3.5, "holes": None, "hole_d": None},
}

CELL_18650 = {"diameter": 18.6, "length": 65.2}


def _lookup(table: dict, kind: str, what: str) -> dict:
    if kind not in table:
        raise ValueError(f"unknown {what} {kind!r}; known: {sorted(table)}")
    return table[kind]


def _span(bottom: float, top: float, length: float, width: float, x: float = 0.0):
    return cq.Workplane("XY").workplane(offset=bottom).center(x, 0).rect(length, width).extrude(top - bottom)


def servo(kind: str = "SG90") -> cq.Workplane:
    """A hobby servo. Origin on the output shaft axis at the underside of the mounting flange.

    The case hangs below z=0, the shaft points up +Z, and the case extends toward -X from the shaft.
    """
    s = _lookup(SERVO_TABLE, kind, "servo")
    centre = -s["shaft_offset"]
    below, above = -s["flange_z"], s["height"] - s["flange_z"]
    body = _span(below, above, s["length"], s["width"], centre)
    flange = _span(0.0, s["flange_thick"], s["flange_length"], s["width"], centre)
    for x, y in s["holes"]:
        flange = flange.cut(cq.Workplane("XY").center(centre + x, y).circle(s["hole_d"] / 2).extrude(s["flange_thick"]))
    boss = cq.Workplane("XY").workplane(offset=above).circle(s["boss_d"] / 2).extrude(s["boss_h"])
    shaft = cq.Workplane("XY").workplane(offset=above + s["boss_h"]).circle(s["shaft_d"] / 2).extrude(s["shaft_h"])
    return body.union(flange).union(boss).union(shaft)


def servo_horn(kind: str = "SG90", arm: float = 16.0) -> cq.Workplane:
    """The stock single-arm horn. Origin on the shaft axis at the top of the shaft; the arm points +X, the hub rises."""
    s = _lookup(SERVO_TABLE, kind, "servo")
    if arm <= 0:
        raise ValueError("a horn needs a positive arm length")
    hub_d, thick = s["shaft_d"] + 2.8, 1.6
    hub = cq.Workplane("XY").circle(hub_d / 2).extrude(thick + 2.0)
    tip = hub_d * 0.45
    points = [(0, hub_d / 2), (arm, tip / 2), (arm, -tip / 2), (0, -hub_d / 2)]
    return hub.union(cq.Workplane("XY").polyline(points).close().extrude(thick))


def servo_hole_points(kind: str = "SG90") -> list[tuple[float, float]]:
    """Mounting hole centres, in the frame of :func:`servo`."""
    s = _lookup(SERVO_TABLE, kind, "servo")
    return [(x - s["shaft_offset"], y) for x, y in s["holes"]]


def servo_cutout(kind: str = "SG90", clearance: float = 0.3, screw_hole: float | None = None) -> cq.Workplane:
    """What to cut from a plate whose top face is at the servo's z=0, so the flange rests on it.

    Place it with the same translation and rotation as the :func:`servo` it receives.
    """
    s = _lookup(SERVO_TABLE, kind, "servo")
    bottom, top = -s["flange_z"] - 1.0, 0.05
    cutter = _span(bottom, top, s["length"] + 2 * clearance, s["width"] + 2 * clearance, -s["shaft_offset"])
    radius = (screw_hole if screw_hole is not None else s["hole_d"]) / 2
    for x, y in servo_hole_points(kind):
        cutter = cutter.union(cq.Workplane("XY").workplane(offset=bottom).center(x, y).circle(radius).extrude(top - bottom))
    return cutter


def board(kind: str) -> cq.Workplane:
    """A circuit board, centred on the origin, bottom face at z=0, components on top as one block."""
    b = _lookup(BOARD_TABLE, kind, "board")
    pcb = _span(0.0, b["thick"], b["length"], b["width"])
    if b["holes"]:
        for x, y in board_hole_points(kind):
            pcb = pcb.cut(cq.Workplane("XY").center(x, y).circle(b["hole_d"] / 2).extrude(b["thick"]))
    inset = (b["holes"][0] if b["holes"] else b["length"]) - 2 * (b["hole_d"] or 1.0)
    width = (b["holes"][1] if b["holes"] else b["width"]) - 2 * (b["hole_d"] or 1.0)
    return pcb.union(_span(b["thick"], b["thick"] + b["parts_h"], inset, width))


def board_hole_points(kind: str) -> list[tuple[float, float]]:
    """Mounting hole centres in the frame of :func:`board`; empty when the board has no standard pattern."""
    b = _lookup(BOARD_TABLE, kind, "board")
    if not b["holes"]:
        return []
    dx, dy = b["holes"][0] / 2, b["holes"][1] / 2
    return [(-dx, -dy), (dx, -dy), (-dx, dy), (dx, dy)]


def cell_18650() -> cq.Workplane:
    """An 18650 cell lying along X, centred on the origin."""
    return cq.Workplane("YZ").workplane(offset=-CELL_18650["length"] / 2).circle(CELL_18650["diameter"] / 2).extrude(CELL_18650["length"])


def screw(size: str = "M3", length: float = 10.0) -> cq.Workplane:
    """A cap-head screw. Origin under the head: the head rises to +Z and the shank runs down -Z."""
    s = _lookup(SCREW_TABLE, size, "screw size")
    if length <= 0:
        raise ValueError("a screw needs a positive length")
    head = cq.Workplane("XY").circle(s["cap_head_dia"] / 2).extrude(s["cap_head_h"])
    shank = cq.Workplane("XY").workplane(offset=-length).circle(float(size[1:]) / 2).extrude(length)
    return head.union(shank)


def nut(size: str = "M3") -> cq.Workplane:
    """A hex nut, centred on the origin axis, bottom face at z=0."""
    n = _lookup(NUT_TABLE, size, "nut size")
    across_corners = n["flat"] / math.cos(math.radians(30))
    return (cq.Workplane("XY").polygon(6, across_corners).circle(float(size[1:]) / 2).extrude(n["thick"]))
