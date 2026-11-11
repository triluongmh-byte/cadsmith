"""Command line: build, check, fit, plate, render, web."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from cadsmith import __version__
from cadsmith.bambu3mf import make_project
from cadsmith.dotenv import read_env
from cadsmith.export import export_part
from cadsmith.fit import check_fits
from cadsmith.loader import load_model
from cadsmith.mesh import tessellate
from cadsmith.model import DEFAULT_CLEARANCE
from cadsmith.plates import DEFAULT_GAP, DEFAULT_PRINTER, DEFAULT_SIZE_RATIO, PRINTERS, get_printer
from cadsmith.printability import DEFAULT_MIN_WALL, DEFAULT_OVERHANG_DEG, DEFAULT_WARN_WALL, check_part
from cadsmith.render import render_meshes


def _build(args) -> int:
    model = load_model(args.model)
    out = Path(args.out)
    for part in model.printed:
        paths = export_part(part.shape, part.name, out)
        print(f"{part.name}: " + ", ".join(str(path) for path in paths.values()))
    return 0


def _print_check(report) -> None:
    mark = "PASS" if report.passed else "FAIL"
    print(f"{report.part}: {mark}, best face down: {report.chosen}, {report.samples} surface points scanned")
    for result in report.orientations:
        chosen = "  <- chosen" if result.down == report.chosen else ""
        fits = "" if result.fits_bed else "  (too big)"
        print(f"  {result.down} down: overhang {result.overhang_area:>9.1f} mm2 ({result.overhang_percent:>5.1f}%), "
              f"bed contact {result.bed_contact:>8.1f} mm2, height {result.height:.1f} mm{fits}{chosen}")
    for error in report.errors:
        print(f"  ERROR: {error}")
    for warning in report.warnings:
        print(f"  warning: {warning}")


def _check(args) -> int:
    model = load_model(args.model)
    printer = get_printer(args.printer)
    bed = (printer.width, printer.depth, printer.height)
    reports = [check_part(part.name, part.shape, min_wall=args.min_wall, warn_wall=max(args.warn_wall, args.min_wall),
                          overhang_deg=args.overhang, bed=bed, down=part.down) for part in model.printed]
    if args.json:
        print(json.dumps([report.to_dict() for report in reports], indent=2))
    else:
        for report in reports:
            _print_check(report)
    return 0 if all(report.passed for report in reports) else 1


def _fit(args) -> int:
    model = load_model(args.model)
    results = check_fits(model, args.clearance)
    if args.json:
        print(json.dumps([result.to_dict() for result in results], indent=2))
    else:
        if not results:
            print("only one part, nothing to fit")
        for result in results:
            print(("PASS " if result.passed else "FAIL ") + result.message)
    return 0 if all(result.passed for result in results) else 1


def _plate(args) -> int:
    models = [load_model(path) for path in args.model]
    printer = get_printer(args.printer)
    plates = make_project(models, printer, args.out, project=args.name, gap=args.gap, size_ratio=args.size_ratio)
    for plate in plates:
        names = ", ".join(placement.item.name for placement in plate.placements)
        print(f"{plate.name}: {names}")
    print(f"wrote {args.out} ({len(plates)} plate{'s' if len(plates) != 1 else ''}, {printer.name}, one filament)")
    return 0


def _render(args) -> int:
    model = load_model(args.model)
    path = Path(args.out) / f"{model.name}.png"
    render_meshes([tessellate(part.shape) for part in model.parts], path)
    print(path)
    return 0


def _web(args) -> int:
    from cadsmith.web.server import serve

    serve(Path(args.dir).expanduser(), args.port, args.printer, git_token=args.git_token)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cadsmith", description="Parametric CadQuery parts, checked and laid out for Bambu printers.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    printers = sorted(PRINTERS)

    build = commands.add_parser("build", help="export each part to STEP, STL and 3MF")
    build.add_argument("model")
    build.add_argument("-o", "--out", default="out")
    build.set_defaults(run=_build)

    check = commands.add_parser("check", help="scan for thin walls and measure overhang in six orientations")
    check.add_argument("model")
    check.add_argument("--min-wall", type=float, default=DEFAULT_MIN_WALL, help="thinnest wall allowed, mm")
    check.add_argument("--warn-wall", type=float, default=DEFAULT_WARN_WALL, help="warn below this, mm")
    check.add_argument("--overhang", type=float, default=DEFAULT_OVERHANG_DEG, help="largest unsupported angle from vertical")
    check.add_argument("--printer", default=DEFAULT_PRINTER, choices=printers)
    check.add_argument("--json", action="store_true")
    check.set_defaults(run=_check)

    fit = commands.add_parser("fit", help="measure the clearance between every pair of parts")
    fit.add_argument("model")
    fit.add_argument("--clearance", type=float, default=DEFAULT_CLEARANCE, help="for pairs the model does not declare, mm")
    fit.add_argument("--json", action="store_true")
    fit.set_defaults(run=_fit)

    plate = commands.add_parser("plate", help="arrange parts on named plates in one Bambu 3MF")
    plate.add_argument("model", nargs="+")
    plate.add_argument("-o", "--out", default="out/plates.3mf")
    plate.add_argument("--printer", default=DEFAULT_PRINTER, choices=printers)
    plate.add_argument("--name", help="prefix for plate names; defaults to the first model's name")
    plate.add_argument("--gap", type=float, default=DEFAULT_GAP, help="space between parts, mm")
    plate.add_argument("--size-ratio", type=float, default=DEFAULT_SIZE_RATIO,
                       help="a part shares a plate only if its footprint is at least this share of the plate's largest")
    plate.set_defaults(run=_plate)

    render = commands.add_parser("render", help="draw a PNG preview of the assembled model")
    render.add_argument("model")
    render.add_argument("-o", "--out", default="out")
    render.set_defaults(run=_render)

    web = commands.add_parser("web", help="open the local studio: chat on the right, live preview on the left")
    # A .env here may hold CADSMITH_HOME and GITHUB_TOKEN. The token goes to git only, never to the agent.
    dotenv = read_env(Path(".env"))
    web.add_argument("--dir", default=os.environ.get("CADSMITH_HOME") or dotenv.get("CADSMITH_HOME") or "cadsmith-workspace",
                     help="folder holding your projects; defaults to $CADSMITH_HOME. Make it a git repository to sync it")
    web.set_defaults(git_token=dotenv.get("GITHUB_TOKEN"))
    web.add_argument("--port", type=int, default=8420)
    web.add_argument("--printer", default=DEFAULT_PRINTER, choices=printers)
    web.set_defaults(run=_web)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.run(args)
    except ValueError as error:
        print(f"cadsmith: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
