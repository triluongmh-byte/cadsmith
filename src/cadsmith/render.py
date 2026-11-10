"""Plain PNG previews from a small software z-buffer, with no GPU or display."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from cadsmith.mesh import Mesh  # noqa: E402
from cadsmith.plates import Plate, Printer  # noqa: E402

COLORS = ("#e8743b", "#3b8ea5", "#7a9e3b", "#b0589a")
_LIGHT = np.array([0.35, -0.55, 0.75])
_SUPERSAMPLE = 2
_NORMAL_EDGE = 0.25


def _camera(elev: float, azim: float) -> np.ndarray:
    """World-to-view rotation: rows are the view's right, up and toward-viewer axes."""
    e, a = np.radians(elev), np.radians(azim)
    toward = np.array([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)])
    right = np.cross([0.0, 0.0, 1.0], toward)
    right /= np.linalg.norm(right)
    return np.stack([right, np.cross(toward, right), toward])


def _rasterize(triangles: np.ndarray, shades: np.ndarray, normals: np.ndarray, ids: np.ndarray, size: int):
    """A z-buffer fill: per pixel colour, normal and depth (inf where nothing was drawn)."""
    colour = np.ones((size, size, 3))
    normal = np.zeros((size, size, 3))
    part = -np.ones((size, size), dtype=int)
    depth = np.full((size, size), np.inf)
    for tri, shade, face_normal, part_id in zip(triangles, shades, normals, ids):
        x0, y0 = np.maximum(np.floor(tri[:, :2].min(axis=0)).astype(int), 0)
        x1, y1 = np.minimum(np.ceil(tri[:, :2].max(axis=0)).astype(int) + 1, size)
        if x1 <= x0 or y1 <= y0:
            continue
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = tri
        area = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)
        if abs(area) < 1e-9:
            continue
        grid_x, grid_y = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
        w0 = ((bx - grid_x) * (cy - grid_y) - (cx - grid_x) * (by - grid_y)) / area
        w1 = ((cx - grid_x) * (ay - grid_y) - (ax - grid_x) * (cy - grid_y)) / area
        w2 = 1 - w0 - w1
        inside = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9)
        z = w0 * az + w1 * bz + w2 * cz
        window = depth[y0:y1, x0:x1]
        nearer = inside & (z < window - 1e-6)
        window[nearer] = z[nearer]
        colour[y0:y1, x0:x1][nearer] = shade
        normal[y0:y1, x0:x1][nearer] = face_normal
        part[y0:y1, x0:x1][nearer] = part_id
    return colour, normal, depth, part


def _outline(colour: np.ndarray, normal: np.ndarray, depth: np.ndarray, part: np.ndarray, pitch: float) -> np.ndarray:
    """Darken pixels on silhouettes, part boundaries and sharp creases, so edges read clearly."""
    finite = np.isfinite(depth)
    edges = np.zeros(depth.shape, dtype=bool)
    for axis in (0, 1):
        near_d, far_d = np.moveaxis(depth, axis, 0)[:-1], np.moveaxis(depth, axis, 0)[1:]
        near_n, far_n = np.moveaxis(normal, axis, 0)[:-1], np.moveaxis(normal, axis, 0)[1:]
        near_p, far_p = np.moveaxis(part, axis, 0)[:-1], np.moveaxis(part, axis, 0)[1:]
        with np.errstate(invalid="ignore"):
            jump = np.abs(near_d - far_d) > 4 * pitch
        crease = np.linalg.norm(near_n - far_n, axis=-1) > _NORMAL_EDGE
        found = jump | crease | (near_p != far_p)
        mask = np.moveaxis(edges, axis, 0)
        mask[:-1] |= found
    edges &= finite | np.roll(finite, 1, axis=0) | np.roll(finite, 1, axis=1)
    result = colour.copy()
    result[edges] = result[edges] * 0.45
    return result


def render_meshes(meshes: list[Mesh], path: str | Path, *, elev: float = 24, azim: float = -58, size: int = 900) -> Path:
    """Draw meshes together in one orthographic view, each in its own colour."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    view = _camera(elev, azim)
    light = _LIGHT / np.linalg.norm(_LIGHT)
    triangles, shades, normals, ids = [], [], [], []
    for index, mesh in enumerate(meshes):
        base = np.array(matplotlib.colors.to_rgb(COLORS[index % len(COLORS)]))
        face_normals = mesh.normals
        brightness = 0.5 + 0.5 * np.clip(face_normals @ light, 0, 1)
        triangles.append(mesh.triangles @ view.T)
        shades.append(np.clip(base[None, :] * brightness[:, None], 0, 1))
        normals.append(face_normals)
        ids.append(np.full(len(mesh.faces), index))
    projected = np.concatenate(triangles)
    span = (projected[..., :2].max(axis=(0, 1)) - projected[..., :2].min(axis=(0, 1))).max()
    pixels = size * _SUPERSAMPLE
    pitch = span * 1.1 / pixels
    centre = (projected[..., :2].max(axis=(0, 1)) + projected[..., :2].min(axis=(0, 1))) / 2
    screen = projected.copy()
    screen[..., 0] = (projected[..., 0] - centre[0]) / pitch + pixels / 2
    screen[..., 1] = (centre[1] - projected[..., 1]) / pitch + pixels / 2
    screen[..., 2] = -projected[..., 2]
    colour, normal, depth, part = _rasterize(screen, np.concatenate(shades), np.concatenate(normals), np.concatenate(ids), pixels)
    image = _outline(colour, normal, depth, part, pitch)
    image = image.reshape(size, _SUPERSAMPLE, size, _SUPERSAMPLE, 3).mean(axis=(1, 3))
    plt.imsave(path, np.clip(image, 0, 1))
    return path


def render_plates(plates: list[Plate], printer: Printer, path: str | Path, *, size: float = 3.2) -> Path:
    """A top view of every plate, with parts and exclusion zones to scale."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    figure, axes = plt.subplots(1, len(plates), figsize=(size * len(plates), size + 0.3), dpi=150, squeeze=False)
    for plate, ax in zip(plates, axes[0]):
        ax.add_patch(Rectangle((0, 0), printer.width, printer.depth, facecolor="#f4f4f2", edgecolor="#555555"))
        for x0, y0, x1, y1 in printer.exclude:
            ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, facecolor="#d9d9d6", edgecolor="none"))
        for index, placement in enumerate(plate.placements):
            ax.add_patch(Rectangle((placement.x, placement.y), placement.item.width, placement.item.depth,
                                   facecolor=COLORS[index % len(COLORS)], edgecolor="#333333", alpha=0.85))
            if placement.item.width >= printer.width * 0.15:
                ax.text(placement.x + placement.item.width / 2, placement.y + placement.item.depth / 2, placement.item.name,
                        ha="center", va="center", fontsize=7)
        ax.set_title(plate.name, fontsize=9)
        ax.set_xlim(-4, printer.width + 4)
        ax.set_ylim(-4, printer.depth + 4)
        ax.set_aspect("equal")
        ax.set_axis_off()
    figure.savefig(path, bbox_inches="tight", pad_inches=0.05, facecolor="white")
    plt.close(figure)
    return path
