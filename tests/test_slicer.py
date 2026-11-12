import json
import sys
from pathlib import Path

import pytest

from cadsmith.web import server as server_module
from cadsmith.web import slicer as slicer_module
from cadsmith.web.slicer import Slicer, find_slicer
from test_web import GOOD, get, post, running, wait_idle  # noqa: F401  (running is a fixture)


@pytest.fixture
def no_slicer_installed(monkeypatch, tmp_path):
    monkeypatch.delenv("CADSMITH_SLICER", raising=False)
    for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / "empty"))
    monkeypatch.setattr(slicer_module, "_registered", lambda executable: None)
    monkeypatch.setattr(slicer_module.shutil, "which", lambda command: None)
    monkeypatch.setattr(slicer_module.Path, "home", lambda: tmp_path / "home")
    return tmp_path


def test_a_named_slicer_is_used(no_slicer_installed, monkeypatch):
    program = no_slicer_installed / "orca-slicer.exe"
    program.write_bytes(b"")
    monkeypatch.setenv("CADSMITH_SLICER", str(program))
    assert find_slicer() == Slicer("Orca Slicer", program)
    monkeypatch.setenv("CADSMITH_SLICER", str(no_slicer_installed / "missing.exe"))
    assert find_slicer() is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows install folders")
def test_bambu_studio_is_found_in_program_files_first(no_slicer_installed, monkeypatch):
    folder = no_slicer_installed / "Programs"
    for relative in ("Bambu Studio/bambu-studio.exe", "OrcaSlicer/orca-slicer.exe"):
        (folder / relative).parent.mkdir(parents=True)
        (folder / relative).write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(folder))
    found = find_slicer()
    assert found.name == "Bambu Studio" and found.program == folder / "Bambu Studio" / "bambu-studio.exe"


def test_nothing_installed_means_no_slicer(no_slicer_installed):
    assert find_slicer() is None


def test_a_mac_app_opens_through_open(monkeypatch):
    monkeypatch.setattr(slicer_module.sys, "platform", "darwin")
    command = Slicer("Bambu Studio", Path("/Applications/BambuStudio.app")).command(Path("/tmp/Pen box.3mf"))
    assert command == ["open", "-a", str(Path("/Applications/BambuStudio.app")), str(Path("/tmp/Pen box.3mf"))]


def test_the_studio_opens_the_latest_build_in_the_slicer(running, monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(server_module.slicer_module, "find_slicer", lambda: Slicer("Bambu Studio", tmp_path / "bambu.exe"))
    monkeypatch.setattr(server_module.slicer_module, "open_in_slicer", lambda found, file: opened.append(file))
    studio, url = running()
    root = url.rsplit("/p/", 1)[0]
    assert json.loads(get(f"{root}/api/slicer")[1]) == {"name": "Bambu Studio"}
    assert post(f"{url}/api/open", {})[0] == 400  # nothing built yet
    post(f"{url}/api/chat", {"message": "make a block"})
    wait_idle(studio)
    assert post(f"{url}/api/open", {}) == (200, {"slicer": "Bambu Studio", "file": "project.3mf"})
    file = opened[0]
    assert file.parent.parent.name == f"v{studio.report['version']}" and file.read_bytes().startswith(b"PK")


def test_without_a_slicer_the_page_offers_the_download(running, monkeypatch):
    monkeypatch.setattr(server_module.slicer_module, "find_slicer", lambda: None)
    studio, url = running()
    root = url.rsplit("/p/", 1)[0]
    assert json.loads(get(f"{root}/api/slicer")[1]) == {"name": None}
    assert post(f"{url}/api/open", {})[0] == 404
