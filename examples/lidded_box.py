"""A box with a drop-in lid. The lid's lip keeps a stated clearance from the cavity."""

import cadquery as cq

from cadsmith import Fit
from cadsmith.cadlib.enclosure import hollow_box


class Params:
    length = 80.0
    width = 60.0
    height = 35.0
    wall = 2.4
    lid_thickness = 3.0
    lip_height = 6.0
    lip_wall = 1.6
    clearance = 0.3  # per side: the one number both halves are built from


p = Params()


def _base() -> cq.Workplane:
    return hollow_box(length=p.length, width=p.width, height=p.height, wall=p.wall).translate((0, 0, p.height / 2))


def _lid() -> cq.Workplane:
    """Built in place: the plate rests on the rim, the lip hangs into the cavity."""
    top = cq.Workplane("XY").workplane(offset=p.height).rect(p.length, p.width).extrude(p.lid_thickness)
    lip_length = p.length - 2 * p.wall - 2 * p.clearance
    lip_width = p.width - 2 * p.wall - 2 * p.clearance
    lip = (cq.Workplane("XY").workplane(offset=p.height - p.lip_height).rect(lip_length, lip_width)
           .rect(lip_length - 2 * p.lip_wall, lip_width - 2 * p.lip_wall).extrude(p.lip_height))
    return top.union(lip)


def parts():
    return {"base": _base(), "lid": _lid()}


# The lid sits on the rim along Z; every side gap must still be at least the clearance.
FITS = [Fit("base", "lid", min_clearance=p.clearance, seat="z")]
ORIENT = {"base": "-Z", "lid": "+Z"}
