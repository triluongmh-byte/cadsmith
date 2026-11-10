"""Do the parts fit: the clearance between every pair, measured on the B-rep in assembled pose.

Every pair is checked, at the default clearance unless a :class:`cadsmith.Fit` says otherwise, so
an undeclared collision cannot hide.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import combinations

import numpy as np
from OCP.BRepExtrema import BRepExtrema_DistShapeShape

from cadsmith.model import DEFAULT_CLEARANCE, AXES, Fit, Model

MIN_OVERLAP_MM3 = 1e-3
TOLERANCE = 1e-4
_ALONG = 0.999
_SPAN_TOLERANCE = 0.01


@dataclass
class FitResult:
    a: str
    b: str
    required: float
    measured: float | None
    overlap_mm3: float
    passed: bool
    message: str
    location: tuple[float, float, float] | None = None
    seat: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _overlap(a, b) -> float:
    common = a.intersect(b)
    return float(sum(solid.Volume() for solid in common.Solids()))


def _closest(a, b):
    tool = BRepExtrema_DistShapeShape(a.wrapped, b.wrapped)
    if not tool.IsDone() or tool.NbSolution() < 1:
        raise RuntimeError("the distance between two faces could not be measured")
    p = tool.PointOnShape1(1)
    q = tool.PointOnShape2(1)
    return float(tool.Value()), np.array([p.X(), p.Y(), p.Z()]), np.array([q.X(), q.Y(), q.Z()])


def _on_seat(box_a: np.ndarray, box_b: np.ndarray, p: np.ndarray, q: np.ndarray, axis: int) -> bool:
    """True when two faces only meet end to end along the seat axis.

    A face normal to the axis, or a side wall stacked on another, shares no span of that axis
    with its partner. Side walls that run alongside each other do, so they are still measured.
    """
    if min(box_a[axis + 3], box_b[axis + 3]) - max(box_a[axis], box_b[axis]) > _SPAN_TOLERANCE:
        return False
    offset = q - p
    length = float(np.linalg.norm(offset))
    return length < 1e-6 or abs(float(offset[axis])) / length > _ALONG


def _boxes(faces) -> np.ndarray:
    boxes = [face.BoundingBox() for face in faces]
    return np.array([[b.xmin, b.ymin, b.zmin, b.xmax, b.ymax, b.zmax] for b in boxes])


def _smallest_gap(a, b, required: float, seat: str | None):
    """The smallest gap that counts, ignoring contact on the seat: (gap, location) or (None, None)."""
    seat_index = None if seat is None else AXES.index(seat)
    faces_a, faces_b = a.Faces(), b.Faces()
    boxes_a, boxes_b = _boxes(faces_a), _boxes(faces_b)
    best, where = None, None
    margin = required + 1.0
    for index, face_a in enumerate(faces_a):
        box = boxes_a[index]
        near = np.all(boxes_b[:, :3] <= box[3:] + margin, axis=1) & np.all(boxes_b[:, 3:] >= box[:3] - margin, axis=1)
        for other in np.flatnonzero(near):
            face_b = faces_b[other]
            value, p, q = _closest(face_a, face_b)
            if seat_index is not None and _on_seat(boxes_a[index], boxes_b[other], p, q, seat_index):
                continue
            if best is None or value < best:
                best, where = value, (p + q) / 2
    return best, where


def _measure(a_name, a, b_name, b, required: float, seat: str | None) -> FitResult:
    pair = f"{a_name} / {b_name}"
    overlap = _overlap(a, b)
    if overlap > MIN_OVERLAP_MM3:
        return FitResult(a_name, b_name, required, 0.0, round(overlap, 3), False,
                         f"{pair}: the parts overlap by {overlap:.2f} mm3", seat=seat)
    gap, where = _smallest_gap(a, b, required, seat)
    if gap is None:
        # Nothing but seat contact is within reach of the required clearance.
        return FitResult(a_name, b_name, required, None, 0.0, True,
                         f"{pair}: seated on {seat}, nothing else within {required + 1:g} mm", seat=seat)
    passed = gap >= required - TOLERANCE
    location = tuple(round(float(c), 2) for c in where)
    verdict = "ok" if passed else "too tight"
    return FitResult(a_name, b_name, required, round(gap, 4), 0.0, passed,
                     f"{pair}: {gap:.3f} mm gap, needs {required:g} mm ({verdict})", location, seat)


def check_fits(model: Model, default_clearance: float = DEFAULT_CLEARANCE) -> list[FitResult]:
    """Measure every pair of parts in the model."""
    declared: dict[frozenset, Fit] = {fit.key: fit for fit in model.fits}
    results = []
    for first, second in combinations(model.parts, 2):
        fit = declared.get(frozenset((first.name, second.name)))
        required = fit.min_clearance if fit else default_clearance
        results.append(_measure(first.name, first.shape, second.name, second.shape, required, fit.seat if fit else None))
    return results
