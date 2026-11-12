"""The local web studio: many projects, each with one agent and one build at a time.

It binds to localhost only and refuses requests from other sites, because a chat message here
ends up editing and running code on this machine.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from cadsmith.stl import StlError, describe_stl
from cadsmith.web.agent import ClaudeAgent
from cadsmith.web import slicer as slicer_module
from cadsmith.web.pipeline import MODEL_FILE, PLATES_FILE, REPORT_FILE
from cadsmith.web.sync import GitSync, SyncError

logger = logging.getLogger("cadsmith.web")
STATIC = Path(__file__).parent / "static"
MAX_BODY = 64 * 1024
MAX_QUEUED = 10
MAX_UPLOAD = 100 * 1024 * 1024
UPLOAD_DIR = "uploads"
#: Picture formats the agent can look at, by the bytes they start with.
IMAGE_MAGIC = {".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
               ".gif": (b"GIF87a", b"GIF89a"), ".webp": (b"RIFF",)}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
MAX_MESSAGE = 8000
MAX_REPAIRS = 3
MAX_REVIEWS = 2
MAX_PRINTING_NOTES = 12
RECAP_CHARS = 12000
BUILD_TIMEOUT = 300
DEFAULT_PORT = 8420
SERVED_SUFFIXES = {".stl": "model/stl", ".step": "application/step", ".3mf": "model/3mf", ".py": "text/plain; charset=utf-8",
                   ".svg": "image/svg+xml"}
#: What new projects used to start with; a project still holding exactly this counts as empty.
OLD_STARTER_MODEL = '''import cadquery as cq


class Params:
    length = 60.0
    width = 40.0
    height = 20.0
    fillet = 3.0


p = Params()


def parts():
    return {"block": cq.Workplane("XY").box(p.length, p.width, p.height, centered=(True, True, False)).edges("|Z").fillet(p.fillet)}
'''


META_FILE = "project.json"
PROJECT_FILES = (MODEL_FILE, "builds", "events.jsonl")
MAX_NAME = 60
#: A project folder, named after the project; older ones are ten hex digits.
ID_PATTERN = r"[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?"
OLD_ID_PATTERN = r"[0-9a-f]{10}"
MAX_FOLDER = 40


def folder_name(name: str) -> str:
    """A folder name that reads like the project: 'Hộp bút 2' becomes 'hop-but-2'."""
    text = unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "D"))
    text = text.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:MAX_FOLDER].strip("-") or "project"


class Busy(RuntimeError):
    """A turn is already running."""


class ProjectError(ValueError):
    """A project request that can be explained to the person who made it."""


def send_gelf(address: str, payload: dict) -> None:
    """Send one GELF message over UDP to ``host:port``."""
    host, _, port = address.rpartition(":")
    message = {"version": "1.1", "host": socket.gethostname(), "short_message": f"POST {payload['path']}",
               **{f"_{key}": value for key, value in payload.items()}}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(json.dumps(message).encode()[:8000], (host, int(port)))


def save_upload(workspace: Path, filename: str, data: bytes) -> dict:
    """Keep a picture or an STL in the project's uploads folder; returns what the chat says about it."""
    original = Path(filename or "").name
    suffix = Path(original).suffix.lower()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(original).stem).strip("-")[:40] or "upload"
    if suffix == ".stl":
        try:
            info = describe_stl(data).to_dict()
        except StlError as problem:
            raise ProjectError(str(problem)) from None
        kind = "mesh"
    elif suffix in IMAGE_MAGIC:
        if not data.startswith(IMAGE_MAGIC[suffix]) or (suffix == ".webp" and data[8:12] != b"WEBP"):
            raise ProjectError(f"that file is not a {suffix[1:].upper()} picture")
        info, kind = {}, "image"
    else:
        raise ProjectError("send a picture (PNG, JPG, GIF, WebP) or an STL mesh")
    folder = workspace / UPLOAD_DIR
    folder.mkdir(exist_ok=True)
    target, count = folder / f"{stem}{suffix}", 1
    while target.exists():
        count += 1
        target = folder / f"{stem}-{count}{suffix}"
    target.write_bytes(data)
    return {"path": f"{UPLOAD_DIR}/{target.name}", "name": original, "kind": kind, "bytes": len(data), **info}


def log_post(path: str, request, response, error) -> None:
    """Record a POST for debugging. Goes to Graylog when ``CADSMITH_GRAYLOG=host:port`` is set."""
    payload = {"path": path, "request": json.dumps(request, ensure_ascii=False)[:4000],
               "response": json.dumps(response, ensure_ascii=False)[:4000], "error": error or ""}
    logger.info("POST %s request=%s response=%s error=%s", path, payload["request"], payload["response"], payload["error"])
    address = os.environ.get("CADSMITH_GRAYLOG")
    if address:
        try:
            send_gelf(address, payload)
        except (OSError, ValueError) as failure:
            logger.warning("could not reach Graylog: %s", failure)


def _preview_path(report: dict) -> str | None:
    return f"builds/v{report['version']}/{report['preview']}" if report.get("preview") else None


def repair_prompt(report: dict, problems: str) -> str:
    """Ask the agent to fix a build that failed its checks."""
    preview = _preview_path(report)
    look = f" Read {preview} first to see what you built." if preview else ""
    return f"The model you just wrote did not pass.{look} Fix these problems in model.py:\n{problems}"


def printing_notes(report: dict) -> str:
    """What passed the checks but would still make the print worse."""
    notes = [f"{part['name']}: {warning}" for part in report["parts"] for warning in part["check"]["warnings"]]
    if not notes:
        return ""
    shown = "\n".join(notes[:MAX_PRINTING_NOTES])
    return ("\nPrinting notes from the checks. Supports that a different ORIENT or a split would avoid, and structural "
            f"walls under 1.6 mm, count as defects to fix:\n{shown}\n")


def review_prompt(report: dict, request: str) -> str:
    """Ask the agent to look at what it built and judge it against the request."""
    preview = _preview_path(report)
    size = " x ".join(f"{value:g}" for value in report["size"])
    if not preview:
        return (f"v{report['version']} passed the automatic checks but no picture could be drawn. Its overall size is "
                f"{size} mm. If that does not match the request, fix model.py; if it does, do not touch model.py.")
    return f"""v{report['version']} passed the automatic checks. Its overall size is {size} mm.
{printing_notes(report)}Use Read on {preview}: iso, front, right and top views, with bought parts in grey. Each view is scaled to fill its frame.
Judge it as a designer, against this request and your plan: {request!r}
Compare the overall size with every number in the request: a difference over 1 mm must be fixed.
Does it look like what was asked, and would someone name it from the top view alone? Is anything floating, misplaced or passing through
another part? Could it really be assembled and work? What looks blocky or unfinished that one edit would lift?
If it needs work, edit model.py. If it is right, leave model.py untouched.
Finish with one or two sentences telling the user what they now have, in their language. Do not mention this review."""


def describe_problems(report: dict) -> str:
    """What must be fixed, as text for the agent. Empty when the build is clean."""
    if not report.get("ok"):
        return "The model failed to build:\n" + report.get("error", "unknown error")
    lines = [f"{part['name']}: {error}" for part in report["parts"] for error in part["check"]["errors"]]
    lines += [fit["message"] + (f" near {tuple(fit['location'])}" if fit.get("location") else "")
              for fit in report["fits"] if not fit["passed"]]
    if report.get("plate_error"):
        lines.append(report["plate_error"])
    return "\n".join(lines)


class Studio:
    def __init__(self, workspace: Path, printer: str = "x1c", agent_command: list[str] | None = None,
                 on_idle: Callable[["Studio"], None] | None = None):
        self.printer = printer
        self.on_idle = on_idle  # told when a turn ends, so the projects folder can be committed
        self._use_folder(workspace.resolve())
        self.builds.mkdir(parents=True, exist_ok=True)
        self._clear_old_starter()
        self._lock = threading.Lock()
        self.events = self._load_events()
        self.status = "idle"
        self.started = 0.0
        self.pending: list[str] = []  # messages sent while the agent works, read at its next step
        self._turn_from = 0  # where the running turn's events start
        self.report = None
        self.error = None
        self.agent = ClaudeAgent(self.workspace, agent_command, self._load_session())
        self._restore_latest()

    def _use_folder(self, workspace: Path) -> None:
        self.workspace = workspace
        self.builds = workspace / "builds"
        self.model = workspace / MODEL_FILE
        self._meta_file = workspace / META_FILE
        self._session_file = workspace / "session.json"
        self._events_file = workspace / "events.jsonl"

    def move_to(self, workspace: Path) -> None:
        """Move the project to another folder. Claude Code keeps a conversation per folder, so the agent
        starts a new one there and gets a recap of the chat."""
        with self._lock:
            if self.status != "idle":
                raise Busy("this project is still working; stop it first")
            shutil.move(str(self.workspace), str(workspace))
            self._use_folder(workspace)
            self._session_file.unlink(missing_ok=True)
            self.agent.workspace, self.agent.session_id = workspace, None

    def _clear_old_starter(self) -> None:
        """Empty a project that only ever held the old placeholder block."""
        if not self.model.exists() or self.model.read_text(encoding="utf-8") != OLD_STARTER_MODEL:
            return
        self.model.unlink()
        shutil.rmtree(self.builds)
        self.builds.mkdir()
        if '"kind": "user"' not in (self._events_file.read_text(encoding="utf-8") if self._events_file.exists() else ""):
            self._events_file.unlink(missing_ok=True)

    @property
    def is_empty(self) -> bool:
        return not self.model.exists()

    @property
    def id(self) -> str:
        return self.workspace.name

    @property
    def name(self) -> str:
        try:
            return json.loads(self._meta_file.read_text(encoding="utf-8"))["name"]
        except (OSError, ValueError, KeyError):
            return self.workspace.name

    @name.setter
    def name(self, value: str) -> None:
        self._meta_file.write_text(json.dumps({"name": value}, ensure_ascii=False), encoding="utf-8")

    def summary(self) -> dict:
        with self._lock:
            report = self.report
            return {"id": self.id, "name": self.name, "status": self.status, "version": report and report["version"],
                    "passed": report and report["passed"],
                    "updated": (self.workspace if self.is_empty else self.model).stat().st_mtime}

    def _load_session(self) -> str | None:
        try:
            return json.loads(self._session_file.read_text(encoding="utf-8")).get("session_id")
        except (OSError, ValueError):
            return None

    def _load_events(self) -> list[dict]:
        if not self._events_file.exists():
            return []
        events = []
        for line in self._events_file.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    def _versions(self) -> list[int]:
        found = (re.fullmatch(r"v(\d+)", path.name) for path in self.builds.iterdir())
        return sorted(int(match.group(1)) for match in found if match)

    def _restore_latest(self) -> None:
        for version in reversed(self._versions()):
            try:
                report = json.loads((self.builds / f"v{version}" / REPORT_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if report.get("ok"):
                self.report = report
                return

    def project_file(self) -> Path:
        """The latest build's project 3MF, under the project's name, which the slicer shows as its title."""
        with self._lock:
            report = self.report
        source = report and self.builds / f"v{report['version']}" / PLATES_FILE
        if not source or not source.is_file():
            raise ProjectError("there is no build to open yet")
        safe = "".join(c for c in self.name if c.isalnum() or c in " _-").strip() or "project"
        target = self.builds / f"v{report['version']}" / "open" / f"{safe}.3mf"
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, target)
        return target

    def _build_matches_model(self) -> bool:
        if self.report is None or self.is_empty:
            return self.is_empty
        built = self.builds / f"v{self.report['version']}" / MODEL_FILE
        return built.exists() and built.read_bytes() == self.model.read_bytes()

    def refresh(self) -> None:
        """Read the project again after another computer changed it, and build a model this one has not built."""
        with self._lock:
            if self.status != "idle":
                return
            self.events = self._load_events()
            self.report = self.error = None
            self.agent.session_id = self._load_session()
        self._restore_latest()
        if not self._build_matches_model():
            with self._lock:
                self.report = None
            self.rebuild()

    def _recap(self, before: int) -> str:
        """The conversation so far, for an agent that cannot resume it, such as on another computer."""
        lines = [f"{'User' if event['kind'] == 'user' else 'You'}: {event['text']}"
                 for event in self.events[:before] if event.get("kind") in ("user", "text")]
        text = "\n\n".join(lines)[-RECAP_CHARS:]
        if not text:
            return ""
        return ("You are picking up this project without your earlier conversation. "
                f"This is how it went, latest last:\n\n{text}\n\nThe current model.py is the result.\n\n")

    def emit(self, kind: str, text: str) -> None:
        with self._lock:
            event = {"seq": len(self.events) + 1, "kind": kind, "text": text}
            self.events.append(event)
        with self._events_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def state(self, since: int = 0) -> dict:
        with self._lock:
            return {"status": self.status, "elapsed": round(time.time() - self.started) if self.status != "idle" else 0,
                    "events": self.events[since:], "seq": len(self.events), "report": self.report, "error": self.error,
                    "queued": len(self.pending),
                    "workspace": str(self.workspace), "id": self.id, "name": self.name, "empty": self.is_empty}

    def build(self) -> dict:
        """Build the model as the next version and publish it if it produced geometry."""
        versions = self._versions()
        version = (versions[-1] if versions else 0) + 1
        try:
            subprocess.run([sys.executable, "-m", "cadsmith.web.pipeline", str(self.workspace), str(version), self.printer, self.name],
                           capture_output=True, timeout=BUILD_TIMEOUT)
            report = json.loads((self.builds / f"v{version}" / REPORT_FILE).read_text(encoding="utf-8"))
        except subprocess.TimeoutExpired:
            report = {"ok": False, "version": version, "passed": False, "error": f"the build took over {BUILD_TIMEOUT} s"}
        except (OSError, ValueError) as error:
            report = {"ok": False, "version": version, "passed": False, "error": str(error)}
        with self._lock:
            if report.get("ok"):
                self.report, self.error = report, None
            else:
                self.error = report.get("error", "the build failed")
        return report

    def build_initial(self) -> None:
        """Build a model that has no build yet, such as after a restart."""
        if self.report is None:
            self.rebuild()

    def rebuild(self) -> None:
        """Build the current model again, unless there is none or a turn is already running."""
        with self._lock:
            if self.status != "idle" or self.is_empty:
                return
            self.status, self.started = "working", time.time()
        self._turn(None)

    def send(self, message: str) -> bool:
        """Start a turn, or queue the message for the running one. Returns True when it was queued."""
        with self._lock:
            busy = self.status != "idle"
            if busy and len(self.pending) >= MAX_QUEUED:
                raise Busy(f"{MAX_QUEUED} messages are already waiting; let the agent catch up")
            if busy:
                self.pending.append(message)
            else:
                self.status, self.started = "working", time.time()
                self._turn_from = len(self.events)
        self.emit("user", message)
        if busy:
            self.emit("note", "Queued. The agent reads this at its next step.")
        else:
            threading.Thread(target=self._turn, args=(message,), daemon=True).start()
        return busy

    def stop(self) -> bool:
        with self._lock:
            dropped, self.pending = len(self.pending), []
        stopped = self.agent.stop()
        if dropped:
            self.emit("note", f"Dropped {dropped} queued message{'s' if dropped > 1 else ''}.")
        return stopped

    def _take_pending(self) -> str | None:
        with self._lock:
            taken, self.pending = self.pending, []
        return "\n\n".join(taken) if taken else None

    def _turn(self, message: str | None) -> None:
        while True:
            try:
                self._run_turn(message)
            except Exception as error:  # the turn runs on its own thread; nothing else would report this
                logger.exception("turn failed")
                self.emit("error", str(error))
            with self._lock:
                idle = not self.pending  # checked under the lock, so a message sent now starts a turn of its own
                if idle:
                    self.status = "idle"
                else:
                    message, self.pending = "\n\n".join(self.pending), []
            if idle:
                if self.on_idle:
                    self.on_idle(self)
                return

    def _save_session(self) -> None:
        if self.agent.session_id:
            self._session_file.write_text(json.dumps({"session_id": self.agent.session_id}), encoding="utf-8")

    def _ask_agent(self, prompt: str):
        """Run the agent, handing it the conversation so far when it has none to resume."""
        if self.agent.session_id is None:
            return self.agent.run(self._recap(self._turn_from) + prompt, self.emit)
        result = self.agent.run(prompt, self.emit)
        if not result.ok and "no conversation found" in (result.error or "").lower():
            self.agent.session_id = None  # Claude Code keeps conversations per computer and folder
            self._session_file.unlink(missing_ok=True)
            return self.agent.run(self._recap(self._turn_from) + prompt, self.emit)
        return result

    def _run_turn(self, message: str | None) -> None:
        """Let the agent work, build what it wrote, then have it fix problems and review the result."""
        prompt, problems = message, ""
        repairs = reviews = 0
        while True:
            added = self._take_pending() if prompt is not None and prompt is not message else None
            if added:  # the user wrote while the agent worked: hand it over at this step
                prompt += f"\n\nThe user added this while you were working. It comes first:\n{added}"
                message = f"{message}\n\n{added}" if message else added
                repairs = reviews = 0
            if prompt is not None:
                before = None if self.is_empty else self.model.read_bytes()
                result = self._ask_agent(prompt)
                self._save_session()
                if not result.ok:
                    self.emit("error", "Stopped." if result.error == "stopped" else result.error)
                    return
                if self.is_empty or self.model.read_bytes() == before:
                    if problems:
                        self.emit("error", f"v{report['version']} still has problems:\n{problems}")
                    elif reviews:
                        self.emit("done", f"v{report['version']} built, checked and reviewed")
                    return  # a question, not a change
            self.emit("build", "the model and run every check")
            report = self.build()
            problems = describe_problems(report)
            if problems:
                if message is None or repairs == MAX_REPAIRS:
                    self.emit("error", f"v{report['version']} still has problems:\n{problems}")
                    return
                repairs += 1
                self.emit("note", f"v{report['version']} has problems, asking for a fix:\n{problems}")
                prompt = repair_prompt(report, problems)
            elif message is None or reviews == MAX_REVIEWS:
                self.emit("done", f"v{report['version']} built, all checks passed")
                return
            else:
                reviews += 1
                self.emit("note", f"v{report['version']} passed the checks. Comparing the result with your request.")
                prompt = review_prompt(report, message)


class Projects:
    """Every project under one folder, each in a folder named after it. The folder name is its id."""

    def __init__(self, root: Path, printer: str = "x1c", agent_command: list[str] | None = None,
                 git_token: str | None = None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.printer, self.agent_command = printer, agent_command
        self._lock = threading.Lock()
        self._migrate_single_project()
        self.git = GitSync(self.root, git_token)
        try:
            self.git.sync("Sync on start")  # take the other computer's work before opening anything
        except SyncError as failure:
            logger.warning("could not sync the projects folder: %s", failure)
        self.studios: dict[str, Studio] = {path.name: self._open(path) for path in self._folders()}
        self._name_old_folders()

    def _free_folder(self, name: str, ignore: Path | None = None) -> Path:
        base = folder_name(name)
        for count in range(1, 1000):
            path = self.root / (base if count == 1 else f"{base[:MAX_FOLDER - 4]}-{count}")
            if path == ignore or not path.exists():
                return path
        raise ProjectError("too many projects with that name")

    def _move(self, studio: Studio, target: Path) -> None:
        """Move a project to ``target`` (the caller holds the lock)."""
        old = studio.id
        studio.move_to(target)
        self.studios[studio.id] = self.studios.pop(old)

    def _name_old_folders(self) -> None:
        """Give projects from before named folders a folder named after them."""
        moved = []
        with self._lock:
            for studio in list(self.studios.values()):
                if re.fullmatch(OLD_ID_PATTERN, studio.id) and folder_name(studio.name) != studio.id:
                    target = self._free_folder(studio.name)
                    self._move(studio, target)
                    moved.append(studio.name)
        if moved:
            self._save_in_background("Name folders after their projects: " + ", ".join(moved))

    def _folders(self) -> list[Path]:
        return [path for path in self.root.iterdir()
                if path.is_dir() and re.fullmatch(ID_PATTERN, path.name) and ((path / META_FILE).exists() or (path / MODEL_FILE).exists())]

    def _open(self, path: Path) -> Studio:
        return Studio(path, self.printer, self.agent_command, on_idle=self._turn_ended)

    def _turn_ended(self, studio: Studio) -> None:
        version = studio.report and studio.report.get("version")
        self._save(f"{studio.name}: v{version}" if version else studio.name)

    def _save(self, message: str) -> None:
        if self.git.save(message):
            self._reopen()

    def _save_in_background(self, message: str) -> None:
        if self.git.enabled:
            _background(lambda: self._save(message))

    def sync(self) -> dict:
        """Commit, pull and push, then open what the other computer added or changed."""
        if not self.git.enabled:
            raise ProjectError("the projects folder is not a git repository")
        with self._lock:
            if any(studio.status != "idle" for studio in self.studios.values()):
                raise Busy("a project is still working; sync when it is done")
        changed = self.git.sync()
        if changed:
            self._reopen()
        return {"changed": changed, **self.git.status()}

    def _reopen(self) -> None:
        with self._lock:
            found = {path.name: path for path in self._folders()}
            for key in [key for key, studio in self.studios.items() if key not in found and studio.status == "idle"]:
                del self.studios[key]  # deleted on the other computer
            opened = [self.studios.setdefault(key, self._open(path)) for key, path in found.items() if key not in self.studios]
            kept = [studio for studio in self.studios.values() if studio not in opened]
        for studio in opened:
            _background(studio.build_initial)
        for studio in kept:
            _background(studio.refresh)

    def _migrate_single_project(self) -> None:
        """Move a workspace from before projects existed into a project of its own."""
        if not (self.root / MODEL_FILE).exists():
            return
        target = self.root / uuid.uuid4().hex[:10]
        target.mkdir()
        for name in PROJECT_FILES:
            if (self.root / name).exists():
                shutil.move(str(self.root / name), str(target / name))
        # Claude Code ties a conversation to its folder, so the old one cannot be resumed from here.
        (self.root / "session.json").unlink(missing_ok=True)
        (target / META_FILE).write_text(json.dumps({"name": "My first project"}), encoding="utf-8")

    def _clean_name(self, name, ignore: str | None = None) -> str:
        cleaned = " ".join(str(name or "").split())
        if not cleaned or len(cleaned) > MAX_NAME or not cleaned.isprintable():
            raise ProjectError(f"a project name needs 1 to {MAX_NAME} printable characters")
        taken = {studio.name.casefold() for key, studio in self.studios.items() if key != ignore}
        if cleaned.casefold() in taken:
            raise ProjectError(f"there is already a project called {cleaned}")
        return cleaned

    def list(self) -> list[dict]:
        with self._lock:
            studios = list(self.studios.values())
        return sorted((studio.summary() for studio in studios), key=lambda item: -item["updated"])

    def get(self, project_id: str) -> Studio | None:
        with self._lock:
            return self.studios.get(project_id)

    def create(self, name) -> Studio:
        with self._lock:
            cleaned = self._clean_name(name)
            path = self._free_folder(cleaned)
            path.mkdir()
            (path / META_FILE).write_text(json.dumps({"name": cleaned}, ensure_ascii=False), encoding="utf-8")
            studio = self._open(path)
            self.studios[studio.id] = studio
        self._save_in_background(f"New project {cleaned}")
        return studio

    def rename(self, studio: Studio, name) -> str:
        """Rename a project and move it to a folder named after it."""
        with self._lock:
            cleaned = self._clean_name(name, ignore=studio.id)
            old = studio.name
            target = self._free_folder(cleaned, ignore=studio.workspace)
            if target != studio.workspace:
                self._move(studio, target)
            studio.name = cleaned
        if studio.is_empty:  # a model is rebuilt after a rename and saved when that ends
            self._save_in_background(f"Rename {old} to {cleaned}")
        return cleaned

    def delete(self, studio: Studio) -> None:
        """Move a project to ``.trash`` in the root folder, so a slip can still be recovered by hand."""
        with self._lock:
            if studio.status != "idle":
                raise Busy("this project is still working; stop it first")
            trash = self.root / ".trash"
            trash.mkdir(exist_ok=True)
            name = studio.name
            target = trash / studio.id
            if target.exists():  # a project of the same name was deleted before
                target = trash / f"{studio.id}-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.move(str(studio.workspace), str(target))
            del self.studios[studio.id]
        self._save_in_background(f"Delete {name}")


def _background(target) -> None:
    threading.Thread(target=target, daemon=True).start()


def make_handler(projects: Projects, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cadsmith"

        def log_message(self, *args):  # POSTs are logged by log_post; polling would only be noise
            pass

        def _send(self, code: int, body: bytes, content_type: str, sandbox: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            if sandbox:  # build files come from model code: never let one run script on this origin
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, value) -> None:
            self._send(code, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _trusted(self) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                return False
            origin = self.headers.get("Origin")
            return origin is None or urlsplit(origin).netloc in allowed_hosts

        def _home(self) -> None:
            listed = projects.list()
            studio = projects.get(listed[0]["id"]) if listed else projects.create("My first project")
            if not listed:
                _background(studio.build_initial)
            self._redirect(f"/p/{studio.id}/")

        def do_GET(self):
            if not self._trusted():
                return self._json(403, {"error": "forbidden"})
            url = urlsplit(self.path)
            if url.path == "/":
                return self._home()
            if url.path == "/api/projects":
                return self._json(200, {"projects": projects.list()})
            if url.path == "/api/sync":
                return self._json(200, projects.git.status())
            if url.path == "/api/slicer":
                found = slicer_module.find_slicer()
                return self._json(200, {"name": found and found.name})
            match = re.fullmatch(rf"/p/({ID_PATTERN})(/.*)", url.path)
            studio = projects.get(match.group(1)) if match else None
            if studio is None:
                return self._json(404, {"error": "not found"})
            rest = match.group(2)
            if rest == "/":
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if rest == "/api/state":
                try:
                    since = max(0, int(parse_qs(url.query).get("since", ["0"])[0]))
                except ValueError:
                    return self._json(400, {"error": "since must be a number"})
                return self._json(200, studio.state(since))
            picture = re.fullmatch(r"/uploads/([A-Za-z0-9_-]+\.[a-z]+)", rest)
            if picture:
                path = studio.workspace / UPLOAD_DIR / picture.group(1)
                if path.suffix in IMAGE_TYPES and path.is_file():
                    return self._send(200, path.read_bytes(), IMAGE_TYPES[path.suffix], sandbox=True)
                return self._json(404, {"error": "not found"})
            found = re.fullmatch(r"/files/(v\d+)/([A-Za-z0-9_.\- ]+)", rest.replace("%20", " "))
            if found:
                path = (studio.builds / found.group(1) / found.group(2)).resolve()
                if path.is_relative_to(studio.builds) and path.is_file() and path.suffix in SERVED_SUFFIXES:
                    return self._send(200, path.read_bytes(), SERVED_SUFFIXES[path.suffix], sandbox=True)
            return self._json(404, {"error": "not found"})

        def _route_post(self, request: dict) -> tuple[int, dict]:
            if self.path == "/api/projects":
                studio = projects.create(request.get("name"))
                _background(studio.build_initial)
                return 201, {"id": studio.id, "name": studio.name}
            if self.path == "/api/sync":
                try:
                    return 200, projects.sync()
                except SyncError as failure:
                    return 409, {"error": str(failure)}
            match = re.fullmatch(rf"/p/({ID_PATTERN})/api/(chat|stop|rename|delete|open)", self.path)
            studio = projects.get(match.group(1)) if match else None
            if studio is None:
                return 404, {"error": "not found"}
            action = match.group(2)
            if action == "chat":
                message = str(request.get("message", "")).strip()
                if not message or len(message) > MAX_MESSAGE:
                    return 400, {"error": f"send a message of 1 to {MAX_MESSAGE} characters"}
                return 202, {"ok": True, "queued": studio.send(message)}
            if action == "stop":
                return 200, {"stopped": studio.stop()}
            if action == "open":
                found = slicer_module.find_slicer()
                if found is None:
                    return 404, {"error": "no slicer found on this computer; download the 3MF instead"}
                file = studio.project_file()
                slicer_module.open_in_slicer(found, file)
                return 200, {"slicer": found.name, "file": file.name}
            if action == "rename":
                name = projects.rename(studio, request.get("name"))
                _background(studio.rebuild)  # plate names inside the 3MF follow the project name
                return 200, {"id": studio.id, "name": name}
            projects.delete(studio)
            return 200, {"deleted": studio.id}

        def do_POST(self):
            request, response, error, code = None, None, None, 500
            try:
                if not self._trusted():
                    code, response = 403, {"error": "forbidden"}
                    return
                upload = re.fullmatch(rf"/p/({ID_PATTERN})/api/upload", self.path)
                if upload and self.headers.get_content_type() == "application/octet-stream":
                    length = int(self.headers.get("Content-Length") or 0)
                    name = unquote(self.headers.get("X-Filename", ""))
                    request = {"file": name, "bytes": length}  # the log keeps what came, not the bytes
                    studio = projects.get(upload.group(1))
                    if studio is None:
                        code, response = 404, {"error": "not found"}
                    elif length <= 0 or length > MAX_UPLOAD:
                        code, response = 413, {"error": f"send a file of up to {MAX_UPLOAD // (1024 * 1024)} MB"}
                    else:
                        code, response = 201, save_upload(studio.workspace, name, self.rfile.read(length))
                    return
                if self.headers.get_content_type() != "application/json":
                    code, response = 415, {"error": "send JSON"}
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    code, response = 413, {"error": "too large"}
                    return
                request = json.loads(self.rfile.read(length) or b"{}")
                code, response = self._route_post(request)
            except Busy as busy:
                code, response = 409, {"error": str(busy)}
            except ProjectError as problem:
                code, response = 400, {"error": str(problem)}
            except (ValueError, AttributeError) as bad:
                error, code, response = str(bad), 400, {"error": "invalid request"}
            except Exception as failure:
                error, response = str(failure), {"error": "server error"}
                logger.exception("POST failed")
            finally:
                log_post(self.path, request, response, error)
                self._json(code, response)

    return Handler


def create_server(projects: Projects, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """A server on localhost. Pass port 0 to take any free port."""
    server = ThreadingHTTPServer(("127.0.0.1", port), None)
    actual = server.server_address[1]
    server.RequestHandlerClass = make_handler(projects, {f"127.0.0.1:{actual}", f"localhost:{actual}"})
    return server


def serve(root: Path, port: int = DEFAULT_PORT, printer: str = "x1c", git_token: str | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    projects = Projects(root, printer, git_token=git_token)
    server = create_server(projects, port)
    for studio in list(projects.studios.values()):
        _background(studio.build_initial)
    print(f"cadsmith studio: http://127.0.0.1:{server.server_address[1]}  (projects in {projects.root})", flush=True)
    server.serve_forever()
