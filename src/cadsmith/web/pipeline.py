"""Build one version of the workspace model. Runs in its own process, since model files are code."""

from __future__ import annotations

import json
import shutil
import sys
import traceback
from pathlib import Path

from cadsmith.bambu3mf import prepare, write_bambu_3mf
from cadsmith.export import export_part
from cadsmith.fit import check_fits
from cadsmith.loader import load_model
from cadsmith.plates import arrange, get_printer
from cadsmith.printability import check_part

MODEL_FILE = "model.py"
PLATES_FILE = "plates.3mf"
REPORT_FILE = "report.json"


def build_version(workspace: Path, version: int, printer_name: str, name: str | None = None) -> dict:
    """Export, check and arrange the model into ``builds/v<version>``; returns the report."""
    out = workspace / "builds" / f"v{version}"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(workspace / MODEL_FILE, out / MODEL_FILE)
    printer = get_printer(printer_name)
    name = name or workspace.name
    bed = (printer.width, printer.depth, printer.height)
    model = load_model(workspace / MODEL_FILE)
    parts = []
    for part in model.parts:
        export_part(part.shape, part.name, out)
        check = check_part(part.name, part.shape, bed=bed, down=part.down)
        parts.append({"name": part.name, "quantity": part.quantity, "check": check.to_dict()})
    fits = [result.to_dict() for result in check_fits(model)]
    plates, plate_error = [], None
    try:
        arranged = arrange([item for item in prepare(model, printer)], printer, project=name)
        write_bambu_3mf(arranged, printer, out / PLATES_FILE)
        plates = [{"name": plate.name,
                   "parts": [{"name": p.item.name, "x": p.x, "y": p.y, "width": p.item.width, "depth": p.item.depth}
                             for p in plate.placements]}
                  for plate in arranged]
    except ValueError as error:
        plate_error = str(error)
    passed = (all(part["check"]["passed"] for part in parts) and all(fit["passed"] for fit in fits)
              and plate_error is None)
    return {"ok": True, "version": version, "passed": passed, "project": name, "parts": parts, "fits": fits,
            "plates": plates, "plate_error": plate_error, "printer": printer.name,
            "bed": {"width": printer.width, "depth": printer.depth}}


def main(argv: list[str]) -> int:
    workspace, version, printer, name = Path(argv[0]), int(argv[1]), argv[2], argv[3]
    try:
        report = build_version(workspace, version, printer, name)
    except Exception:  # a model file can raise anything; the report carries it back to the agent
        report = {"ok": False, "version": version, "passed": False, "error": traceback.format_exc(limit=6)[-3000:]}
    out = workspace / "builds" / f"v{version}"
    out.mkdir(parents=True, exist_ok=True)
    (out / REPORT_FILE).write_text(json.dumps(report), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
