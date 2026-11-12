"""Find a slicer on this computer and open a project file in it."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Slicer:
    name: str
    program: Path  # the executable, or on macOS the .app bundle

    def command(self, file: Path) -> list[str]:
        if sys.platform == "darwin" and self.program.suffix == ".app":
            return ["open", "-a", str(self.program), str(file)]
        return [str(self.program), str(file)]


#: Bambu Studio first: the project files are written for it. Orca reads them too; the others take the parts.
KNOWN = (
    ("Bambu Studio", ("Bambu Studio/bambu-studio.exe",), ("BambuStudio.app",), ("bambu-studio",)),
    ("OrcaSlicer", ("OrcaSlicer/orca-slicer.exe",), ("OrcaSlicer.app",), ("orca-slicer",)),
    ("PrusaSlicer", ("Prusa3D/PrusaSlicer/prusa-slicer.exe",), ("PrusaSlicer.app", "Original Prusa Drivers/PrusaSlicer.app"),
     ("prusa-slicer",)),
    ("UltiMaker Cura", ("UltiMaker Cura/UltiMaker-Cura.exe",), ("UltiMaker Cura.app",), ("cura",)),
)


def _windows_folders() -> list[Path]:
    names = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")
    folders = [Path(os.environ[name]) for name in names if os.environ.get(name)]
    if os.environ.get("LOCALAPPDATA"):
        folders.append(Path(os.environ["LOCALAPPDATA"]) / "Programs")
    return folders


def _mac_folders() -> list[Path]:
    return [Path("/Applications"), Path.home() / "Applications"]


def _registered(executable: str) -> Path | None:
    """Where Windows' App Paths say a program is installed."""
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}") as key:
                path = Path(str(winreg.QueryValue(key, None)).strip('"'))
        except OSError:
            continue
        if path.is_file():
            return path
    return None


def _find(windows: tuple[str, ...], mac: tuple[str, ...], linux: tuple[str, ...]) -> Path | None:
    if sys.platform == "win32":
        for relative in windows:
            for folder in _windows_folders():
                if (folder / relative).is_file():
                    return folder / relative
            found = _registered(Path(relative).name)
            if found:
                return found
        return None
    if sys.platform == "darwin":
        for bundle in mac:
            for folder in _mac_folders():
                if (folder / bundle).is_dir():
                    return folder / bundle
        return None
    for command in linux:
        found = shutil.which(command)
        if found:
            return Path(found)
    return None


def find_slicer() -> Slicer | None:
    """The slicer to open projects in. ``CADSMITH_SLICER`` names one by path; otherwise the first one installed."""
    chosen = os.environ.get("CADSMITH_SLICER")
    if chosen:
        path = Path(chosen).expanduser()
        return Slicer(path.stem.replace("-", " ").title(), path) if path.exists() else None
    for name, windows, mac, linux in KNOWN:
        found = _find(windows, mac, linux)
        if found:
            return Slicer(name, found)
    return None


def open_in_slicer(slicer: Slicer, file: Path) -> None:
    """Start the slicer on ``file`` without waiting for it, and without tying it to this server."""
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    subprocess.Popen(slicer.command(file), cwd=file.parent, **options)
