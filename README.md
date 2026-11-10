# cadsmith

Parametric CAD parts that are checked before you print them.

You write a part as a short CadQuery `.py` file, by hand or with a coding agent working from a plain
description. cadsmith exports it to STEP, STL and 3MF, measures whether it will print and whether the
parts fit together, and lays everything out on named Bambu Lab plates in one 3MF.

| A single part | Parts that must fit | Named plates in one file |
| --- | --- | --- |
| ![Phone stand](docs/images/phone_stand.png) | ![Box with lid](docs/images/lidded_box.png) | ![Plate layout](docs/images/plates.png) |

## Install

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11 or 3.12.

```sh
uv sync
uv run cadsmith --help
```

## Use it

```sh
uv run cadsmith build  examples/lidded_box.py -o out     # STEP, STL and 3MF for each part
uv run cadsmith check  examples/lidded_box.py            # thin walls and overhang
uv run cadsmith fit    examples/lidded_box.py            # clearance between parts
uv run cadsmith plate  examples/*.py -o out/desk.3mf --name desk --printer x1c
uv run cadsmith render examples/lidded_box.py -o out     # PNG preview
```

`check` and `fit` exit with status 1 when a part fails, so they can gate a script. Add `--json` for
machine-readable results. Printers: `x1c`, `p1s`, `p1p`, `a1`, `a1mini`.

## The studio

```sh
uv run cadsmith web        # then open http://127.0.0.1:8420
```

A local web page: the model fills the middle, with part details and the chat on the right. Describe a
part or a change in any language and the model is rewritten, rebuilt and measured. If a check fails, the
agent gets the measurements and tries again, up to twice.

Each project has its own model, versions and chat. Create, rename and delete them from the top bar.
They live in `cadsmith-workspace/`, and a deleted project moves to `cadsmith-workspace/.trash/`.

The chat uses your signed-in [Claude Code](https://claude.com/claude-code) command line, limited to
reading and editing files in the workspace. The page is served on localhost only and refuses requests
from other sites. Set `CADSMITH_GRAYLOG=host:port` to send a log of each POST to Graylog.

## Model files

A model file is ordinary Python, and it runs with your privileges, so only load files you trust.
Build every part in its **assembled** position:

```python
import cadquery as cq
from cadsmith import Fit

def parts():
    return {"base": make_base(), "lid": make_lid()}

FITS = [Fit("base", "lid", min_clearance=0.3, seat="z")]  # optional
ORIENT = {"lid": "+Z"}                                    # optional: this face goes on the bed
QUANTITY = {"lid": 2}                                     # optional: copies to print
```

A single part can define `gen_step()` or a module-level `result` instead of `parts()`. See
[`examples/`](examples/). Helpers for enclosures, screw posts, magnet pockets, snap fits and mating
clearances are in `cadsmith.cadlib`.

## What is checked

**Thin walls.** The whole surface is sampled about every 1 mm and each point measures the distance
through the material to the opposite face. A wall under 0.8 mm fails and one under 1.2 mm warns (change
these with `--min-wall` and `--warn-wall`). Each thin region is reported with its thickness, area and
location.

**Overhang.** Measured with each of the six faces on the bed: overhanging area beyond 45 degrees from
vertical, area touching the bed, and size. The best orientation is picked unless `ORIENT` sets one.

**Fit.** Every pair of parts is measured on the exact B-rep, at 0.3 mm unless a `Fit` states otherwise,
so a collision nobody declared is still caught. A lid that rests on a rim declares `seat="z"`: touching
is then allowed end to end along Z, while side gaps and any overlap are still enforced.

**Plates.** One filament. Largest parts go first, a part joins a plate only if its footprint is at
least a quarter of the largest part there (`--size-ratio`), so big parts print with big parts and small
with small. Each part is turned with its long side along X and rows fill along X. The printer's exclusion
zone is avoided, and each plate is named, such as `desk 1 big`.

## Limits

- The command line starts from a `.py` file; describing a part in words happens in the studio.
- Short bridges count as overhang, so holes in vertical walls report a small overhang area.
- Fits are checked at rest. Motion, snap deflection and press fits (interference) are not modelled.
- Project files are verified by slicing them with the BambuStudio command line. They set the printer
  and default PLA presets; review the settings in BambuStudio before you print.

## Develop

```sh
uv run pytest
```

One test slices a real project and is skipped when BambuStudio is not installed.

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE) for what was adapted from earlier MIT projects.
