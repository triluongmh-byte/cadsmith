"""Can it be printed: thin walls across the whole part, and overhang in each of six orientations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from cadsmith.mesh import Mesh, tessellate

DEFAULT_MIN_WALL = 0.8
DEFAULT_WARN_WALL = 1.2
DEFAULT_OVERHANG_DEG = 45.0
SAMPLE_SPACING = 1.0
MAX_SAMPLES = 200_000
BED_TOLERANCE = 0.1
MIN_BED_CONTACT = 10.0
SUPPORT_AREA = 1.0
#: A sliver smaller than this (a chamfer tip, say) is a warning, not a failure.
MIN_REGION_AREA = 0.5
#: Two faces form a wall only when they oppose each other within this angle.
OPPOSING_DEG = 35.0

_C, _S = 0.0, 1.0
#: The rotation that puts the named face of the part on the bed (pointing to world -Z).
ORIENTATIONS: dict[str, np.ndarray] = {
    "-Z": np.eye(3),
    "+Z": np.array([[1, 0, 0], [0, -1, 0], [0, 0, -1]], dtype=float),
    "-X": np.array([[_C, 0, -_S], [0, 1, 0], [_S, 0, _C]], dtype=float),
    "+X": np.array([[_C, 0, _S], [0, 1, 0], [-_S, 0, _C]], dtype=float),
    "-Y": np.array([[1, 0, 0], [0, _C, -_S], [0, _S, _C]], dtype=float),
    "+Y": np.array([[1, 0, 0], [0, _C, _S], [0, -_S, _C]], dtype=float),
}


@dataclass
class ThinRegion:
    thickness: float
    area: float
    location: tuple[float, float, float]
    severity: str  # "error" or "warning"


@dataclass
class OrientationResult:
    down: str
    overhang_area: float
    overhang_percent: float
    bed_contact: float
    width: float
    depth: float
    height: float
    fits_bed: bool

    @property
    def stable(self) -> bool:
        return self.bed_contact >= MIN_BED_CONTACT


@dataclass
class PrintReport:
    part: str
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    thin_regions: list[ThinRegion] = field(default_factory=list)
    samples: int = 0
    orientations: list[OrientationResult] = field(default_factory=list)
    chosen: str | None = None
    needs_support: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _samples(mesh: Mesh, spacing: float, limit: int):
    """Points covering every triangle at ``spacing``: (points, normals, area per point)."""
    tri, normals, areas = mesh.triangles, mesh.normals, mesh.areas
    used = areas > 1e-9
    tri, normals, areas = tri[used], normals[used], areas[used]
    edges = np.stack([
        np.linalg.norm(tri[:, 1] - tri[:, 0], axis=1),
        np.linalg.norm(tri[:, 2] - tri[:, 1], axis=1),
        np.linalg.norm(tri[:, 0] - tri[:, 2], axis=1),
    ], axis=1).max(axis=1)
    while True:
        cuts = np.maximum(1, np.ceil(edges / spacing).astype(int))
        if int((cuts**2).sum()) <= limit:
            break
        spacing *= 1.25
    all_points, all_normals, all_weights = [], [], []
    for n in np.unique(cuts):
        selected = cuts == n
        # Barycentric centres of the n*n sub-triangles of a triangle.
        grid = [((3 * i + 1) / (3 * n), (3 * j + 1) / (3 * n)) for i in range(n) for j in range(n - i)]
        grid += [((3 * i + 2) / (3 * n), (3 * j + 2) / (3 * n)) for i in range(n - 1) for j in range(n - 1 - i)]
        u, v = np.array(grid).T
        corners = tri[selected]
        points = (corners[:, 0, None] + u[None, :, None] * (corners[:, 1, None] - corners[:, 0, None])
                  + v[None, :, None] * (corners[:, 2, None] - corners[:, 0, None]))
        all_points.append(points.reshape(-1, 3))
        all_normals.append(np.repeat(normals[selected], len(u), axis=0))
        all_weights.append(np.repeat(areas[selected] / len(u), len(u)))
    return np.concatenate(all_points), np.concatenate(all_normals), np.concatenate(all_weights), spacing


def _locator(mesh: Mesh):
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
    from vtkmodules.vtkFiltersFlowPaths import vtkModifiedBSPTree

    points, cells = vtkPoints(), vtkCellArray()
    for vertex in mesh.vertices:
        points.InsertNextPoint(*map(float, vertex))
    for face in mesh.faces:
        cells.InsertNextCell(3, [int(index) for index in face])
    data = vtkPolyData()
    data.SetPoints(points)
    data.SetPolys(cells)
    locator = vtkModifiedBSPTree()
    locator.SetDataSet(data)
    locator.BuildLocator()
    return locator, data


def wall_thickness(mesh: Mesh, reach: float, spacing: float = SAMPLE_SPACING, limit: int = MAX_SAMPLES):
    """Thickness measured inward from surface samples, where a wall under ``reach`` was found.

    Returns ``(points, thicknesses, weights, sample_count, spacing)`` for the samples that hit an
    opposing face within ``reach``. A ray that first meets a face that does not oppose its own is
    an edge or corner, not a wall, and is not counted.
    """
    from vtkmodules.vtkCommonCore import vtkIdList, vtkPoints

    points, normals, weights, spacing = _samples(mesh, spacing, limit)
    locator, _ = _locator(mesh)
    face_normals = mesh.normals
    opposing = -np.cos(np.radians(OPPOSING_DEG))
    hit_points, cells = vtkPoints(), vtkIdList()
    start_offset = 1e-3
    found_points, found_thickness, found_weights = [], [], []
    for point, normal, weight in zip(points, normals, weights):
        start = point - normal * start_offset
        end = point - normal * reach
        if not locator.IntersectWithLine(start.tolist(), end.tolist(), 1e-7, hit_points, cells):
            continue
        if cells.GetNumberOfIds() == 0:
            continue
        distances = [np.linalg.norm(np.array(hit_points.GetPoint(i)) - point) for i in range(hit_points.GetNumberOfPoints())]
        first = int(np.argmin(distances))
        if float(face_normals[cells.GetId(first)] @ normal) > opposing:
            continue
        found_points.append(point)
        found_thickness.append(distances[first])
        found_weights.append(weight)
    return (np.array(found_points).reshape(-1, 3), np.array(found_thickness),
            np.array(found_weights), len(points), spacing)


def _cluster(points: np.ndarray, cell: float) -> list[np.ndarray]:
    """Indices of ``points`` grouped into connected voxel regions."""
    voxels: dict[tuple, list[int]] = {}
    for index, key in enumerate(map(tuple, np.floor(points / cell).astype(int))):
        voxels.setdefault(key, []).append(index)
    groups, unseen = [], set(voxels)
    while unseen:
        stack, members = [unseen.pop()], []
        while stack:
            x, y, z = key = stack.pop()
            members.extend(voxels[key])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        near = (x + dx, y + dy, z + dz)
                        if near in unseen:
                            unseen.remove(near)
                            stack.append(near)
        groups.append(np.array(members))
    return groups


def find_thin_regions(mesh: Mesh, min_wall: float = DEFAULT_MIN_WALL, warn_wall: float = DEFAULT_WARN_WALL,
                      spacing: float = SAMPLE_SPACING) -> tuple[list[ThinRegion], int]:
    """Every region of the part thinner than ``warn_wall``, thinnest first, and the sample count."""
    reach = max(min_wall, warn_wall)
    points, thickness, weights, count, used = wall_thickness(mesh, reach + 0.01, spacing)
    tolerance = 1e-3
    thin = thickness < reach - tolerance
    points, thickness, weights = points[thin], thickness[thin], weights[thin]
    if not len(points):
        return [], count
    regions = []
    for members in _cluster(points, max(used, 0.5) * 2):
        worst = members[int(np.argmin(thickness[members]))]
        area = float(weights[members].sum()) / 2.0  # a wall is sampled from both of its faces
        value = float(thickness[worst])
        severity = "error" if value < min_wall - tolerance and area >= MIN_REGION_AREA else "warning"
        regions.append(ThinRegion(round(value, 3), round(area, 2),
                                  tuple(round(float(c), 2) for c in points[worst]), severity))
    return sorted(regions, key=lambda region: region.thickness), count


def overhang_by_orientation(mesh: Mesh, overhang_deg: float = DEFAULT_OVERHANG_DEG,
                            bed=(256.0, 256.0, 256.0)) -> list[OrientationResult]:
    """Overhang area, bed contact and size with each of the six faces on the bed.

    The overhang angle is measured from vertical: a wall is 0 degrees, a flat ceiling is 90.
    Downward faces resting on the bed do not count. Short bridges count as overhang.
    """
    limit = -np.sin(np.radians(overhang_deg)) - 1e-6
    total = float(mesh.areas.sum())
    results = []
    for down, rotation in ORIENTATIONS.items():
        placed = mesh.rotated(rotation)
        low, high = placed.bounds
        z_normal, areas = placed.normals[:, 2], placed.areas
        on_bed = (placed.triangles[:, :, 2] <= low[2] + BED_TOLERANCE).all(axis=1)
        overhang = float(areas[(z_normal < limit) & ~on_bed].sum())
        contact = float(areas[(z_normal < -0.999) & on_bed].sum())
        size = high - low
        long_side, short_side = max(size[0], size[1]), min(size[0], size[1])
        fits = long_side <= max(bed[:2]) and short_side <= min(bed[:2]) and size[2] <= bed[2]
        results.append(OrientationResult(down, round(overhang, 2), round(100 * overhang / total, 2), round(contact, 2),
                                         round(float(size[0]), 3), round(float(size[1]), 3), round(float(size[2]), 3), bool(fits)))
    return results


def choose_orientation(results: list[OrientationResult]) -> OrientationResult | None:
    """The stable orientation that fits, with least overhang, then most contact, then lowest."""
    usable = [result for result in results if result.fits_bed and result.stable]
    if not usable:
        return None
    return min(usable, key=lambda r: (round(r.overhang_area), -round(r.bed_contact), r.height))


def _solid_problems(shape) -> list[str]:
    problems = []
    if not shape.isValid():
        problems.append("the geometry is not a valid solid")
    solids = shape.Solids()
    if len(solids) > 1:
        problems.append(f"the part is {len(solids)} separate bodies; they will print as loose pieces")
    if not solids:
        problems.append("the part has no solid body")
    return problems


def check_part(name: str, shape, *, min_wall: float = DEFAULT_MIN_WALL, warn_wall: float = DEFAULT_WARN_WALL,
               overhang_deg: float = DEFAULT_OVERHANG_DEG, bed=(256.0, 256.0, 256.0), down: str | None = None,
               spacing: float = SAMPLE_SPACING) -> PrintReport:
    """Run every printability check on one part."""
    if min_wall <= 0:
        raise ValueError(f"min_wall must be > 0, got {min_wall}")
    if down is not None and down not in ORIENTATIONS:
        raise ValueError(f"down must be one of {sorted(ORIENTATIONS)}, got {down!r}")
    report = PrintReport(part=name, passed=True)
    report.errors.extend(_solid_problems(shape))
    mesh = tessellate(shape)
    report.thin_regions, report.samples = find_thin_regions(mesh, min_wall, warn_wall, spacing)
    for region in report.thin_regions:
        text = f"wall {region.thickness} mm over {region.area} mm2 near {region.location}"
        if region.severity == "error":
            report.errors.append(f"{text}, minimum is {min_wall} mm")
        else:
            report.warnings.append(f"{text}, thin but not below the limit or too small to matter")
    report.orientations = overhang_by_orientation(mesh, overhang_deg, bed)
    if down is not None:
        chosen = next(r for r in report.orientations if r.down == down)
        if not chosen.fits_bed:
            report.errors.append(f"with {down} down the part does not fit the {bed[0]:g} x {bed[1]:g} x {bed[2]:g} bed")
        if not chosen.stable:
            report.warnings.append(f"with {down} down only {chosen.bed_contact} mm2 touches the bed")
    else:
        chosen = choose_orientation(report.orientations)
        if chosen is None:
            report.errors.append("no orientation both fits the bed and sits stably on it")
    if chosen is not None:
        report.chosen = chosen.down
        report.needs_support = chosen.overhang_area >= SUPPORT_AREA
        if report.needs_support:
            report.warnings.append(f"with {chosen.down} down, {chosen.overhang_area} mm2 overhangs past "
                                   f"{overhang_deg:g} degrees and needs support or a bridge")
    report.passed = not report.errors
    return report
