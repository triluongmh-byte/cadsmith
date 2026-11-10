"""Load a CadQuery model file.

A model file is ordinary Python and runs with your privileges, so only load files you trust.
It provides its geometry through one of, in this order:

- ``parts()``: returns ``{name: shape}``, every part in its assembled pose;
- ``gen_step()``: returns one shape, or ``{name: shape}``;
- ``result``: one shape at module level.

A shape is a ``cq.Workplane``, ``cq.Shape`` or ``cq.Assembly``. Optional module-level settings:

- ``FITS``: a list of :class:`cadsmith.Fit`, or ``(a, b)`` / ``(a, b, min_clearance)`` tuples;
- ``ORIENT``: ``{name: "-Z"}``, the face that goes on the bed;
- ``QUANTITY``: ``{name: 2}``, copies to print.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import cadquery as cq

from cadsmith.model import Fit, Model, Part
from cadsmith.printability import ORIENTATIONS


class ModelError(ValueError):
    """The model file does not describe usable geometry."""


def to_shape(value, label: str = "part") -> cq.Shape:
    """One ``cq.Shape`` from anything a model file may return."""
    if isinstance(value, cq.Assembly):
        return value.toCompound()
    if isinstance(value, cq.Workplane):
        shapes = [item for item in value.vals() if isinstance(item, cq.Shape)]
        if not shapes:
            raise ModelError(f"{label}: the Workplane holds no geometry")
        return shapes[0] if len(shapes) == 1 else cq.Compound.makeCompound(shapes)
    if isinstance(value, cq.Shape):
        return value
    raise ModelError(f"{label}: expected a Workplane, Shape or Assembly, got {type(value).__name__}")


def _import(path: Path):
    name = f"_cadsmith_model_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


def _geometry(module, stem: str) -> dict:
    if callable(getattr(module, "parts", None)):
        value = module.parts()
    elif callable(getattr(module, "gen_step", None)):
        value = module.gen_step()
    elif hasattr(module, "result"):
        value = module.result
    else:
        raise ModelError("the model file defines none of parts(), gen_step() or result")
    if not isinstance(value, dict):
        return {stem: value}
    if not value:
        raise ModelError("the model file returned no parts")
    return value


def _fit(entry) -> Fit:
    if isinstance(entry, Fit):
        return entry
    if isinstance(entry, (tuple, list)) and len(entry) in (2, 3):
        return Fit(*entry)
    raise ModelError(f"FITS entry must be a Fit or (a, b[, min_clearance]), got {entry!r}")


def load_model(path: str | Path) -> Model:
    """Run a model file and return its :class:`Model`."""
    path = Path(path).resolve()
    if not path.is_file():
        raise ModelError(f"no such model file: {path}")
    module = _import(path)
    geometry = _geometry(module, path.stem)
    orient = getattr(module, "ORIENT", {})
    quantity = getattr(module, "QUANTITY", {})
    for setting, values in (("ORIENT", orient), ("QUANTITY", quantity)):
        unknown = sorted(set(values) - set(geometry))
        if unknown:
            raise ModelError(f"{setting} names unknown parts {unknown}")
    parts = []
    for name, value in geometry.items():
        down = orient.get(name)
        if down is not None and down not in ORIENTATIONS:
            raise ModelError(f"{name}: ORIENT must be one of {sorted(ORIENTATIONS)}, got {down!r}")
        count = quantity.get(name, 1)
        if not isinstance(count, int) or count < 1:
            raise ModelError(f"{name}: QUANTITY must be a positive integer, got {count!r}")
        parts.append(Part(name=str(name), shape=to_shape(value, str(name)), quantity=count, down=down))
    return Model(name=path.stem, parts=parts, fits=[_fit(item) for item in getattr(module, "FITS", [])])
