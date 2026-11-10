"""A desk phone stand: one side profile extruded across the width.

Printed lying on its side, the profile needs no support.
"""

import cadquery as cq


class Params:
    width = 70.0
    base_depth = 90.0
    wall = 4.0
    back_height = 95.0
    lean_deg = 20.0
    lip_height = 14.0
    phone_slot = 13.0  # the thickness of a phone in a case, plus room
    brace = 5.0


p = Params()


def _profile() -> list[tuple[float, float]]:
    """The side view, drawn in the XZ plane: base, front lip and leaning back."""
    import math

    lean = math.tan(math.radians(p.lean_deg))
    front = p.wall + p.phone_slot
    return [
        (0, 0),
        (p.base_depth, 0),
        (p.base_depth, p.wall),
        (front + p.wall + p.back_height * lean, p.wall + p.back_height),
        (front + p.back_height * lean, p.wall + p.back_height),
        (front, p.wall),
        (p.wall, p.wall),
        (p.wall, p.lip_height),
        (0, p.lip_height),
    ]


def _window() -> list[tuple[float, float]]:
    """A lightening cut through the back brace, leaving ``brace`` of plastic on every side."""
    import math

    lean = math.tan(math.radians(p.lean_deg))
    front = p.wall + p.phone_slot
    floor = p.wall + p.brace
    return [
        (front + p.wall + floor * lean + p.brace, floor),
        (p.base_depth - p.brace * 2.2, floor),
        (front + p.wall + (p.back_height - p.brace * 3.2) * lean + p.brace * 0.6, p.back_height - p.brace * 2.2),
    ]


def parts():
    body = cq.Workplane("XZ").polyline(_profile()).close().polyline(_window()).close().extrude(-p.width)
    return {"phone_stand": body}


ORIENT = {"phone_stand": "+Y"}
