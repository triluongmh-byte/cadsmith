"""What a model file describes: named parts in their assembled pose, and how they must fit."""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_CLEARANCE = 0.3
AXES = ("x", "y", "z")


@dataclass
class Part:
    """One physical part.

    ``shape`` is a ``cq.Shape`` in the assembled pose. ``down`` names the part face that goes on
    the bed (``-Z``, ``+Z``, ``-X``, ``+X``, ``-Y``, ``+Y``); ``None`` picks the best of the six.
    A ``bought`` part, such as a servo or a board, is shown and fit-checked but never printed.
    """

    name: str
    shape: object
    quantity: int = 1
    down: str | None = None
    bought: bool = False


@dataclass(frozen=True)
class Fit:
    """A required gap between two parts.

    ``seat`` names the axis along which the pair may touch: a lid resting on a rim seats on
    ``"z"``. Contact is allowed only on faces normal to that axis, and only along it. Side gaps
    and overlapping volume are always checked.
    """

    a: str
    b: str
    min_clearance: float = DEFAULT_CLEARANCE
    seat: str | None = None

    def __post_init__(self):
        if self.a == self.b:
            raise ValueError(f"a fit needs two different parts, got {self.a!r} twice")
        if self.min_clearance < 0:
            raise ValueError(f"min_clearance must be >= 0, got {self.min_clearance}")
        if self.seat is not None and self.seat not in AXES:
            raise ValueError(f"seat must be one of {AXES} or None, got {self.seat!r}")

    @property
    def key(self) -> frozenset[str]:
        return frozenset((self.a, self.b))


@dataclass
class Model:
    """A named set of parts and the fits declared between them."""

    name: str
    parts: list[Part]
    fits: list[Fit] = field(default_factory=list)

    @property
    def printed(self) -> list[Part]:
        return [part for part in self.parts if not part.bought]

    def __post_init__(self):
        names = [part.name for part in self.parts]
        if not names:
            raise ValueError(f"model {self.name!r} has no parts")
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate part names: {duplicates}")
        seen = set()
        for fit in self.fits:
            missing = [name for name in (fit.a, fit.b) if name not in names]
            if missing:
                raise ValueError(f"fit names unknown parts {missing}; parts are {sorted(names)}")
            if fit.key in seen:
                raise ValueError(f"fit between {fit.a!r} and {fit.b!r} is declared twice")
            seen.add(fit.key)
