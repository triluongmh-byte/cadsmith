import cadquery as cq
import pytest

from cadsmith import Fit, Model, Part
from cadsmith.loader import ModelError, load_model, to_shape

CUBE = "import cadquery as cq\nshape = cq.Workplane('XY').box(10, 10, 10)\n"


def write(tmp_path, body, name="thing.py"):
    path = tmp_path / name
    path.write_text(CUBE + body)
    return path


def test_parts_function_returns_named_parts(tmp_path):
    model = load_model(write(tmp_path, "def parts():\n    return {'a': shape, 'b': shape.translate((30, 0, 0))}\n"))
    assert model.name == "thing"
    assert [part.name for part in model.parts] == ["a", "b"]


def test_gen_step_single_shape_is_named_after_the_file(tmp_path):
    model = load_model(write(tmp_path, "def gen_step():\n    return shape\n"))
    assert [part.name for part in model.parts] == ["thing"]


def test_result_variable_is_accepted(tmp_path):
    assert load_model(write(tmp_path, "result = shape\n")).parts[0].shape.Volume() == pytest.approx(1000)


def test_assembly_is_flattened():
    assembly = cq.Assembly().add(cq.Workplane("XY").box(1, 1, 1))
    assert to_shape(assembly).Volume() == pytest.approx(1)


def test_settings_are_applied(tmp_path):
    body = ("def parts():\n    return {'a': shape, 'b': shape}\n"
            "FITS = [('a', 'b', 0.5)]\nORIENT = {'a': '+Z'}\nQUANTITY = {'b': 3}\n")
    model = load_model(write(tmp_path, body))
    assert model.fits == [Fit("a", "b", 0.5)]
    assert model.parts[0].down == "+Z"
    assert model.parts[1].quantity == 3


@pytest.mark.parametrize("body, message", [
    ("", "none of"),
    ("def parts():\n    return {}\n", "no parts"),
    ("def parts():\n    return {'a': 5}\n", "expected a Workplane"),
    ("result = shape\nORIENT = {'nope': '-Z'}\n", "unknown parts"),
    ("result = shape\nORIENT = {'thing': 'up'}\n", "ORIENT must be"),
    ("result = shape\nQUANTITY = {'thing': 0}\n", "QUANTITY must be"),
    ("result = shape\nFITS = ['x']\n", "FITS entry"),
])
def test_bad_model_files_are_rejected(tmp_path, body, message):
    with pytest.raises(ModelError, match=message):
        load_model(write(tmp_path, body))


def test_missing_file(tmp_path):
    with pytest.raises(ModelError, match="no such"):
        load_model(tmp_path / "absent.py")


def test_model_rejects_duplicates_and_unknown_fit_parts():
    shape = cq.Workplane("XY").box(1, 1, 1).val()
    with pytest.raises(ValueError, match="duplicate"):
        Model("m", [Part("a", shape), Part("a", shape)])
    with pytest.raises(ValueError, match="unknown parts"):
        Model("m", [Part("a", shape), Part("b", shape)], [Fit("a", "c")])
    with pytest.raises(ValueError, match="declared twice"):
        Model("m", [Part("a", shape), Part("b", shape)], [Fit("a", "b"), Fit("b", "a")])
    with pytest.raises(ValueError, match="no parts"):
        Model("m", [])


@pytest.mark.parametrize("kwargs", [{"a": "x", "b": "x"}, {"a": "x", "b": "y", "min_clearance": -1}, {"a": "x", "b": "y", "seat": "w"}])
def test_invalid_fit(kwargs):
    with pytest.raises(ValueError):
        Fit(**kwargs)
