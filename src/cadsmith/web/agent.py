"""Talk to the local Claude Code command line, which edits the workspace model."""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

AGENT_TIMEOUT = 900
SYSTEM_PROMPT = """You edit a parametric 3D-printable CadQuery model in ./model.py. It is the only file you change.

The file must define parts() returning {name: cq.Workplane}, every part in its assembled position, in millimetres.
All dimensions go in a Params class at the top. Optional module-level settings:
- FITS = [Fit("a", "b", min_clearance=0.3, seat="z")], from `from cadsmith import Fit`. seat names the axis along which
  two parts may rest on each other.
- ORIENT = {"name": "-Z"}, the face that goes on the print bed.
- QUANTITY = {"name": 2}, copies to print.
Helpers are in cadsmith.cadlib: enclosure, mounting, cutouts, mechanical, fits, layout, styling, tables.

After every turn the model is built and measured for you: walls under 0.8 mm fail, and every pair of parts needs 0.3 mm
of clearance unless a Fit says otherwise. You cannot run commands. Make the edit, then say what changed in one or two
short sentences, in the language the user writes in."""


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
                     "--strict-mcp-config", "--append-system-prompt", SYSTEM_PROMPT]
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
        timer = threading.Timer(AGENT_TIMEOUT, process.kill)
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
