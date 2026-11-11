"""Talk to the local Claude Code command line, which edits the workspace model."""

from __future__ import annotations

import ast
import json
import subprocess
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

AGENT_TIMEOUT = 2700  # a large assembly can take the agent well over 15 minutes
SYSTEM_PROMPT = """You write and edit a parametric 3D-printable CadQuery model in ./model.py. It is the only file you
change. A new project has no model.py yet: create it.

The file must define parts() returning {name: cq.Workplane}, every part in its assembled position, in millimetres.
All dimensions go in a Params class at the top. Optional module-level settings:
- FITS = [Fit("a", "b", min_clearance=0.3, seat="z")], from `from cadsmith import Fit`. seat names the axis along which
  two parts may rest on each other.
- ORIENT = {"name": "-Z"}, the face that goes on the print bed.
- QUANTITY = {"name": 2}, copies to print.
- BOUGHT = {"name"}, parts that are bought, not printed.
Helpers are in cadsmith.cadlib; their signatures are listed below.

After every turn the model is built and measured for you: walls under 0.8 mm fail, and every pair of parts needs 0.3 mm
of clearance unless a Fit says otherwise. You cannot run commands. You will then be shown what you built and asked to
fix or confirm it. Make the edit, then say what changed in one or two short sentences, in the language the user writes
in. Do not mention builds or checks still to come.

A message may end with notes the user pinned on the model. Each names a part, a point on its surface in model
millimetres and the way that face points. Make the change the note asks for at that spot: find the feature in
model.py from the part and the coordinates, change it, and leave the rest of the model alone.

A message may also list files the user attached, under uploads/. Read a picture to see it: it may be a screenshot
of this model with a problem marked, a sketch, or a photo of a real part to match. An STL is a ready-made mesh,
such as a sculpted figure: load it with import_stl from cadsmith.cadlib.meshes, size it to what the user asks,
and build the printed parts around it (a base, a peg, a mount). Never Read an STL. A mesh can only be moved,
scaled, unioned and cut, so keep its own shape and do the design work in the parts you build."""
GUIDE_FILE = Path(__file__).parent / "design_guide.md"
CADLIB_DIR = Path(__file__).parent.parent / "cadlib"


def cadlib_reference() -> str:
    """Every public cadlib function as a signature and a one-line summary, read without importing CadQuery."""
    lines = []
    for path in sorted(CADLIB_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")]
        tables = [target.id for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                  for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                  if isinstance(target, ast.Name) and target.id.isupper() and not target.id.startswith("_")]
        if not functions and not tables:
            continue
        lines.append(f"cadsmith.cadlib.{path.stem}")
        if tables:
            lines.append("  tables: " + ", ".join(tables))
        for node in functions:
            summary = (ast.get_docstring(node) or "").split("\n")[0]
            lines.append(f"  {node.name}({ast.unparse(node.args)})" + (f": {summary}" if summary else ""))
    return "\n".join(lines)


@lru_cache(maxsize=1)
def system_prompt() -> str:
    """The contract, the design guide and the helper list, as one prompt."""
    return f"{SYSTEM_PROMPT}\n\n{GUIDE_FILE.read_text(encoding='utf-8')}\n# Helpers\n\n{cadlib_reference()}"


@dataclass
class AgentResult:
    ok: bool
    error: str | None = None


def _target(tool_input: dict) -> str:
    for key in ("file_path", "path", "pattern", "command"):
        if key in tool_input:
            return str(tool_input[key])
    return ""


class ClaudeAgent:
    """One continuing conversation with Claude Code, restricted to reading and editing files."""

    def __init__(self, workspace: Path, command: list[str] | None = None, session_id: str | None = None):
        self.workspace = workspace
        self.command = command or ["claude"]
        self.session_id = session_id
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.stopped = False

    def _arguments(self) -> list[str]:
        arguments = [*self.command, "-p", "--output-format", "stream-json", "--verbose",
                     "--permission-mode", "acceptEdits", "--allowedTools", "Read,Edit,Write",
                     "--disallowedTools", "Bash,WebFetch,WebSearch,Agent", "--setting-sources", "project",
                     "--strict-mcp-config", "--append-system-prompt", system_prompt()]
        if self.session_id:
            arguments += ["--resume", self.session_id]
        return arguments

    def _handle(self, line: str, emit: Callable[[str, str], None]) -> str | None:
        """Turn one output line into events. Returns an error message from a failed result."""
        try:
            message = json.loads(line)
        except ValueError:
            return None
        kind = message.get("type")
        if message.get("session_id"):
            self.session_id = message["session_id"]
        if kind == "assistant":
            for block in message.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    emit("text", block["text"].strip())
                elif block.get("type") == "tool_use":
                    target = _target(block.get("input") or {}).replace(f"{self.workspace}/", "")
                    emit("tool", f"{block.get('name', 'Tool')} {target}".strip())
        elif kind == "result" and message.get("is_error"):
            return str(message.get("result") or message.get("subtype") or "the agent reported an error")
        return None

    def run(self, prompt: str, emit: Callable[[str, str], None]) -> AgentResult:
        self.stopped = False
        try:
            process = subprocess.Popen(self._arguments(), cwd=self.workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, bufsize=1)
        except FileNotFoundError:
            return AgentResult(False, f"could not find the {self.command[0]!r} command; install and sign in to Claude Code")
        with self._lock:
            self._process = process
        timed_out = threading.Event()
        timer = threading.Timer(AGENT_TIMEOUT, lambda: (timed_out.set(), process.kill()))
        timer.start()
        failure = None
        try:
            process.stdin.write(prompt)
            process.stdin.close()
            for line in process.stdout:
                failure = self._handle(line, emit) or failure
            process.wait()
            stderr = process.stderr.read()
        finally:
            timer.cancel()
            with self._lock:
                self._process = None
        if self.stopped:
            return AgentResult(False, "stopped")
        if timed_out.is_set():
            return AgentResult(False, f"the agent ran out of time after {AGENT_TIMEOUT // 60} minutes")
        if failure or process.returncode:
            return AgentResult(False, failure or stderr[-600:].strip() or f"the agent exited with {process.returncode}")
        return AgentResult(True)

    def stop(self) -> bool:
        with self._lock:
            if self._process is None:
                return False
            self.stopped = True
            self._process.kill()
            return True
