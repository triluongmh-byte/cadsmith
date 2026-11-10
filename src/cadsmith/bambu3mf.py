"""Write one Bambu Studio project 3MF holding every plate, each with its own name.

The layout matches what BambuStudio itself exports: a root model that references one model file
per object, and ``Metadata/model_settings.config`` assigning instances to named plates.
"""

from __future__ import annotations

import json
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import quoteattr

import numpy as np

from cadsmith.export import CONTENT_TYPES, CORE_NS, mesh_xml, relationships
from cadsmith.mesh import Mesh, tessellate
from cadsmith.model import Model
from cadsmith.plates import DEFAULT_GAP, DEFAULT_MARGIN, DEFAULT_SIZE_RATIO, Item, Plate, Printer, arrange
from cadsmith.printability import ORIENTATIONS, choose_orientation, overhang_by_orientation

#: BambuStudio reads plates only from files whose Application names BambuStudio.
APPLICATION = "BambuStudio-02.07.01.62"
BAMBU_NS = "http://schemas.bambulab.com/package/2021"
PRODUCTION_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
#: Plates sit side by side in one world, spaced by the bed size plus this share of it.
PLATE_GAP_RATIO = 0.2
_SWAP_XY = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], dtype=float)


@dataclass
class PrintableMesh:
    """A part turned for printing: long side on X, centred in XY, resting on z = 0."""

    mesh: Mesh
    part: str
    down: str


def prepare(model: Model, printer: Printer) -> list[Item]:
    """Turn every part of the model for printing, one :class:`Item` per printed copy."""
    bed = (printer.width, printer.depth, printer.height)
    items = []
    for part in model.printed:
        mesh = tessellate(part.shape)
        down = part.down
        if down is None:
            best = choose_orientation(overhang_by_orientation(mesh, bed=bed))
            down = best.down if best else "-Z"
        turned = mesh.rotated(ORIENTATIONS[down])
        low, high = turned.bounds
        if high[1] - low[1] > high[0] - low[0]:
            turned = turned.rotated(_SWAP_XY)
            low, high = turned.bounds
        centre = (low + high) / 2
        settled = turned.translated((-centre[0], -centre[1], -low[2]))
        size = high - low
        for copy in range(1, part.quantity + 1):
            name = part.name if part.quantity == 1 else f"{part.name}_{copy}"
            items.append(Item(name, float(size[0]), float(size[1]), float(size[2]), PrintableMesh(settled, part.name, down)))
    return items


def plate_origin(index: int, count: int, printer: Printer) -> tuple[float, float]:
    """World position of plate ``index`` (from 1) out of ``count``, as Bambu Studio lays plates out."""
    root = count ** 0.5
    columns = round(root) + 1 if root > round(root) else round(root)
    row, column = divmod(index - 1, columns)
    stride_x = printer.width * (1 + PLATE_GAP_RATIO)
    stride_y = printer.depth * (1 + PLATE_GAP_RATIO)
    return column * stride_x, -row * stride_y


def _object_file(object_id: int, mesh: Mesh, stem: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE_NS}" xmlns:BambuStudio="{BAMBU_NS}" '
            f'xmlns:p="{PRODUCTION_NS}" requiredextensions="p">'
            '<metadata name="BambuStudio:3mfVersion">1</metadata><resources>'
            f'<object id="{object_id}" p:UUID="{uuid.uuid5(uuid.NAMESPACE_OID, stem)}" type="model">{mesh_xml(mesh)}</object>'
            '</resources><build/></model>\n')


def _settings_xml(entries: list[dict], plates: list[Plate]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"]
    for entry in entries:
        name = quoteattr(entry["name"])
        lines += [f'  <object id="{entry["id"]}">',
                  f'    <metadata key="name" value={name}/>',
                  '    <metadata key="extruder" value="1"/>',
                  f'    <part id="{entry["mesh_id"]}" subtype="normal_part">',
                  f'      <metadata key="name" value={name}/>',
                  '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>',
                  "    </part>", "  </object>"]
    identify = 100
    for plate in plates:
        lines += ["  <plate>", f'    <metadata key="plater_id" value="{plate.index}"/>',
                  f'    <metadata key="plater_name" value={quoteattr(plate.name)}/>',
                  '    <metadata key="locked" value="false"/>']
        for entry in entries:
            if entry["plate"] != plate.index:
                continue
            identify += 1
            lines += ["    <model_instance>", f'      <metadata key="object_id" value="{entry["id"]}"/>',
                      '      <metadata key="instance_id" value="0"/>',
                      f'      <metadata key="identify_id" value="{identify}"/>', "    </model_instance>"]
        lines.append("  </plate>")
    lines += ["  <assemble>", "  </assemble>", "</config>", ""]
    return "\n".join(lines)


def project_settings(printer: Printer) -> str:
    """The printer and presets the project opens with; BambuStudio needs these to place plates."""
    width, depth = f"{printer.width:g}", f"{printer.depth:g}"
    settings = {
        "printable_area": ["0x0", f"{width}x0", f"{width}x{depth}", f"0x{depth}"],
        "printable_height": f"{printer.height:g}",
        "bed_exclude_area": [item for x0, y0, x1, y1 in printer.exclude
                             for item in (f"{x0:g}x{y0:g}", f"{x1:g}x{y0:g}", f"{x1:g}x{y1:g}", f"{x0:g}x{y1:g}")],
        "printer_model": printer.name,
        "printer_variant": "0.4",
        "nozzle_diameter": ["0.4"],
        "printer_settings_id": f"{printer.name} 0.4 nozzle",
        "print_settings_id": printer.process,
        "filament_settings_id": [printer.filament],
        "filament_type": ["PLA"],
        "filament_colour": ["#F2754E"],
        "flush_volumes_matrix": ["0"],  # BambuStudio crashes on a project without this
    }
    return json.dumps(settings, indent=2) + "\n"


def write_bambu_3mf(plates: list[Plate], printer: Printer, path: str | Path) -> Path:
    """Write the plates to one project file. Every part uses filament 1."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    next_id = 1
    for plate in plates:
        origin = plate_origin(plate.index, len(plates), printer)
        for placement in plate.placements:
            item = placement.item
            mesh_id, object_id = next_id, next_id + 1
            next_id += 2
            entries.append({"id": object_id, "mesh_id": mesh_id, "name": item.name, "plate": plate.index,
                            "mesh": item.payload.mesh, "file": f"/3D/Objects/object_{mesh_id}.model",
                            "x": origin[0] + placement.x + item.width / 2,
                            "y": origin[1] + placement.y + item.depth / 2})
    resources, build = [], []
    for entry in entries:
        resources.append(f'<object id="{entry["id"]}" p:UUID="{uuid.uuid5(uuid.NAMESPACE_OID, str(entry["id"]))}" type="model">'
                         f'<components><component p:path="{entry["file"]}" objectid="{entry["mesh_id"]}" '
                         f'p:UUID="{uuid.uuid5(uuid.NAMESPACE_OID, entry["file"])}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
                         "</components></object>")
        build.append(f'<item objectid="{entry["id"]}" p:UUID="{uuid.uuid5(uuid.NAMESPACE_DNS, str(entry["id"]))}" '
                     f'transform="1 0 0 0 1 0 0 0 1 {entry["x"]:.4f} {entry["y"]:.4f} 0" printable="1"/>')
    root = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE_NS}" xmlns:BambuStudio="{BAMBU_NS}" '
            f'xmlns:p="{PRODUCTION_NS}" requiredextensions="p">'
            f'<metadata name="Application">{APPLICATION}</metadata>'
            '<metadata name="BambuStudio:3mfVersion">1</metadata>'
            f'<resources>{"".join(resources)}</resources><build>{"".join(build)}</build></model>\n')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", CONTENT_TYPES)
        package.writestr("_rels/.rels", relationships(["/3D/3dmodel.model"]))
        package.writestr("3D/3dmodel.model", root)
        package.writestr("3D/_rels/3dmodel.model.rels", relationships([entry["file"] for entry in entries]))
        for entry in entries:
            package.writestr(entry["file"].lstrip("/"), _object_file(entry["mesh_id"], entry["mesh"], entry["file"]))
        package.writestr("Metadata/model_settings.config", _settings_xml(entries, plates))
        package.writestr("Metadata/project_settings.config", project_settings(printer))
    return path


def make_project(models: list[Model], printer: Printer, path: str | Path, *, project: str | None = None,
                 gap: float = DEFAULT_GAP, margin: float = DEFAULT_MARGIN,
                 size_ratio: float = DEFAULT_SIZE_RATIO) -> list[Plate]:
    """Prepare, arrange and write parts of one or more models. Returns the plates."""
    items = [item for model in models for item in prepare(model, printer)]
    plates = arrange(items, printer, project=project or models[0].name, gap=gap, margin=margin, size_ratio=size_ratio)
    write_bambu_3mf(plates, printer, path)
    return plates
