"""Write STEP, STL and plain 3MF files for one part."""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from cadsmith.mesh import ANGULAR_TOLERANCE, LINEAR_TOLERANCE, Mesh, tessellate

CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                 '</Types>\n')
MODEL_REL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def relationships(entries: list[str]) -> str:
    rows = "".join(f'<Relationship Target="{target}" Id="rel-{i}" Type="{MODEL_REL}"/>' for i, target in enumerate(entries, 1))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rows}</Relationships>\n')


def mesh_xml(mesh: Mesh) -> str:
    """The ``<mesh>`` element of a 3MF object."""
    vertices = "".join(f'<vertex x="{x:.5f}" y="{y:.5f}" z="{z:.5f}"/>' for x, y, z in mesh.vertices.tolist())
    triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh.faces.tolist())
    return f"<mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh>"


def write_3mf(mesh: Mesh, path: Path, name: str) -> Path:
    """A standard single-object 3MF, in the part's own coordinates."""
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE_NS}">'
             f'<metadata name="Title">{escape(name)}</metadata><resources>'
             f'<object id="1" name={quoteattr(name)} type="model">{mesh_xml(mesh)}</object>'
             '</resources><build><item objectid="1"/></build></model>\n')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", CONTENT_TYPES)
        package.writestr("_rels/.rels", relationships(["/3D/3dmodel.model"]))
        package.writestr("3D/3dmodel.model", model)
    return path


def export_part(shape, name: str, out_dir: str | Path) -> dict[str, Path]:
    """Write ``<name>.step``, ``<name>.stl`` and ``<name>.3mf`` into ``out_dir``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {suffix: out / f"{name}.{suffix}" for suffix in ("step", "stl", "3mf")}
    shape.exportStep(str(paths["step"]))
    shape.exportStl(str(paths["stl"]), LINEAR_TOLERANCE, ANGULAR_TOLERANCE, ascii=False)
    write_3mf(tessellate(shape), paths["3mf"], name)
    return paths
