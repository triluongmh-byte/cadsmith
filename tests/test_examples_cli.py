import json
import zipfile
from pathlib import Path

import pytest

from cadsmith.cli import main
from cadsmith.fit import check_fits
from cadsmith.loader import load_model
from cadsmith.printability import check_part

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
STAND, BOX = str(EXAMPLES / "phone_stand.py"), str(EXAMPLES / "lidded_box.py")


@pytest.mark.parametrize("path", [STAND, BOX])
def test_examples_pass_printability(path):
    for part in load_model(path).parts:
        report = check_part(part.name, part.shape, down=part.down)
        assert report.passed, report.errors
        assert not report.needs_support


def test_lidded_box_keeps_its_declared_clearance():
    result = check_fits(load_model(BOX))[0]
    assert result.passed
    assert result.required == 0.3
    assert result.measured == pytest.approx(0.3, abs=1e-3)


def test_lidded_box_clearance_is_really_checked(tmp_path):
    source = Path(BOX).read_text().replace("clearance = 0.3", "clearance = 0.1").replace("min_clearance=p.clearance", "min_clearance=0.3")
    edited = tmp_path / "tight_box.py"
    edited.write_text(source)
    assert not check_fits(load_model(edited))[0].passed


def test_cli_build(tmp_path, capsys):
    assert main(["build", BOX, "-o", str(tmp_path)]) == 0
    assert {path.name for path in tmp_path.iterdir()} == {f"{part}.{ext}" for part in ("base", "lid") for ext in ("step", "stl", "3mf")}


def test_cli_check_json(capsys):
    assert main(["check", BOX, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [item["chosen"] for item in data] == ["-Z", "+Z"]
    assert len(data[0]["orientations"]) == 6


def test_cli_check_fails_on_a_thin_wall(tmp_path, capsys):
    model = tmp_path / "thin.py"
    model.write_text("import cadquery as cq\nresult = cq.Workplane('XY').box(30, 30, 20).faces('>Z').shell(-0.4)\n")
    assert main(["check", str(model)]) == 1
    assert "ERROR: wall 0.4" in capsys.readouterr().out


def test_cli_fit(capsys):
    assert main(["fit", BOX]) == 0
    assert "PASS base / lid" in capsys.readouterr().out


def test_cli_plate_puts_everything_in_one_file(tmp_path, capsys):
    target = tmp_path / "all.3mf"
    assert main(["plate", STAND, BOX, "-o", str(target), "--name", "desk"]) == 0
    out = capsys.readouterr().out
    assert "desk 1" in out and "one filament" in out
    with zipfile.ZipFile(target) as package:
        assert b"desk 1" in package.read("Metadata/model_settings.config")


def test_cli_render(tmp_path, capsys):
    assert main(["render", BOX, "-o", str(tmp_path)]) == 0
    png = tmp_path / "lidded_box.png"
    assert png.read_bytes()[:4] == b"\x89PNG"


def test_cli_reports_bad_input(tmp_path, capsys):
    assert main(["check", str(tmp_path / "absent.py")]) == 2
    assert "no such model file" in capsys.readouterr().err
