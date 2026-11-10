"""The local web studio: one workspace, one agent, one build at a time.

It binds to localhost only and refuses requests from other sites, because a chat message here
ends up editing and running code on this machine.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cadsmith.web.agent import ClaudeAgent
from cadsmith.web.pipeline import MODEL_FILE, REPORT_FILE

logger = logging.getLogger("cadsmith.web")
STATIC = Path(__file__).parent / "static"
MAX_BODY = 64 * 1024
MAX_MESSAGE = 8000
MAX_REPAIRS = 2
BUILD_TIMEOUT = 300
DEFAULT_PORT = 8420
SERVED_SUFFIXES = {".stl": "model/stl", ".step": "application/step", ".3mf": "model/3mf", ".py": "text/plain; charset=utf-8"}
STARTER_MODEL = '''import cadquery as cq


class Params:
    length = 60.0
    width = 40.0
    height = 20.0
    fillet = 3.0


p = Params()


def parts():
    return {"block": cq.Workplane("XY").box(p.length, p.width, p.height, centered=(True, True, False)).edges("|Z").fillet(p.fillet)}
'''


class Busy(RuntimeError):
    """A turn is already running."""


def send_gelf(address: str, payload: dict) -> None:
    """Send one GELF message over UDP to ``host:port``."""
    host, _, port = address.rpartition(":")
    message = {"version": "1.1", "host": socket.gethostname(), "short_message": f"POST {payload['path']}",
               **{f"_{key}": value for key, value in payload.items()}}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(json.dumps(message).encode()[:8000], (host, int(port)))


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


def describe_problems(report: dict) -> str:
    """What must be fixed, as text for the agent. Empty when the build is clean."""
    if not report.get("ok"):
        return "The model failed to build:\n" + report.get("error", "unknown error")
    lines = [f"{part['name']}: {error}" for part in report["parts"] for error in part["check"]["errors"]]
    lines += [fit["message"] for fit in report["fits"] if not fit["passed"]]
    if report.get("plate_error"):
        lines.append(report["plate_error"])
    return "\n".join(lines)


class Studio:
    def __init__(self, workspace: Path, printer: str = "x1c", agent_command: list[str] | None = None):
        self.workspace = workspace.resolve()
        self.printer = printer
        self.builds = self.workspace / "builds"
        self.builds.mkdir(parents=True, exist_ok=True)
        self.model = self.workspace / MODEL_FILE
        if not self.model.exists():
            self.model.write_text(STARTER_MODEL, encoding="utf-8")
        self._session_file = self.workspace / "session.json"
        self._events_file = self.workspace / "events.jsonl"
        self._lock = threading.Lock()
        self.events = self._load_events()
        self.status = "idle"
        self.started = 0.0
        self.report = None
        self.error = None
        self.agent = ClaudeAgent(self.workspace, agent_command, self._load_session())
        self._restore_latest()

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
                    "workspace": str(self.workspace)}

    def build(self) -> dict:
        """Build the model as the next version and publish it if it produced geometry."""
        versions = self._versions()
        version = (versions[-1] if versions else 0) + 1
        try:
            subprocess.run([sys.executable, "-m", "cadsmith.web.pipeline", str(self.workspace), str(version), self.printer],
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
        """Give a fresh workspace something to look at."""
        with self._lock:
            if self.report is not None or self.status != "idle":
                return
            self.status, self.started = "working", time.time()
        self._turn(None)

    def send(self, message: str) -> None:
        with self._lock:
            if self.status != "idle":
                raise Busy("the agent is still working")
            self.status, self.started = "working", time.time()
        self.emit("user", message)
        threading.Thread(target=self._turn, args=(message,), daemon=True).start()

    def stop(self) -> bool:
        return self.agent.stop()

    def _turn(self, message: str) -> None:
        try:
            self._run_turn(message)
        except Exception as error:  # the turn runs on its own thread; nothing else would report this
            logger.exception("turn failed")
            self.emit("error", str(error))
        finally:
            with self._lock:
                self.status = "idle"

    def _save_session(self) -> None:
        if self.agent.session_id:
            self._session_file.write_text(json.dumps({"session_id": self.agent.session_id}), encoding="utf-8")

    def _run_turn(self, message: str | None) -> None:
        prompt = message
        for attempt in range(MAX_REPAIRS + 1):
            if prompt is not None:
                before = self.model.read_bytes()
                result = self.agent.run(prompt, self.emit)
                self._save_session()
                if not result.ok:
                    self.emit("error", "Stopped." if result.error == "stopped" else result.error)
                    return
                if self.model.read_bytes() == before:
                    return  # a question, not a change
            self.emit("build", "the model and run every check")
            report = self.build()
            problems = describe_problems(report)
            if not problems:
                self.emit("done", f"v{report['version']} built, all checks passed")
                return
            if message is None or attempt == MAX_REPAIRS:
                self.emit("error", f"v{report['version']} still has problems:\n{problems}")
                return
            self.emit("note", f"v{report['version']} has problems, asking for a fix:\n{problems}")
            prompt = f"The model you just wrote did not pass. Fix these problems in model.py:\n{problems}"


def make_handler(studio: Studio, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cadsmith"

        def log_message(self, *args):  # POSTs are logged by log_post; polling would only be noise
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, value) -> None:
            self._send(code, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _trusted(self) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                return False
            origin = self.headers.get("Origin")
            return origin is None or urlsplit(origin).netloc in allowed_hosts

        def do_GET(self):
            if not self._trusted():
                return self._json(403, {"error": "forbidden"})
            url = urlsplit(self.path)
            if url.path == "/":
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/state":
                try:
                    since = max(0, int(parse_qs(url.query).get("since", ["0"])[0]))
                except ValueError:
                    return self._json(400, {"error": "since must be a number"})
                return self._json(200, studio.state(since))
            match = re.fullmatch(r"/files/(v\d+)/([A-Za-z0-9_.\- ]+)", url.path.replace("%20", " "))
            if match:
                path = (studio.builds / match.group(1) / match.group(2)).resolve()
                if (path.is_relative_to(studio.builds) and path.is_file() and path.suffix in SERVED_SUFFIXES):
                    return self._send(200, path.read_bytes(), SERVED_SUFFIXES[path.suffix])
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            request, response, error, code = None, None, None, 500
            try:
                if not self._trusted():
                    code, response = 403, {"error": "forbidden"}
                    return
                if self.headers.get_content_type() != "application/json":
                    code, response = 415, {"error": "send JSON"}
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    code, response = 413, {"error": "too large"}
                    return
                request = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/chat":
                    message = str(request.get("message", "")).strip()
                    if not message or len(message) > MAX_MESSAGE:
                        code, response = 400, {"error": f"send a message of 1 to {MAX_MESSAGE} characters"}
                        return
                    studio.send(message)
                    code, response = 202, {"ok": True}
                elif self.path == "/api/stop":
                    code, response = 200, {"stopped": studio.stop()}
                else:
                    code, response = 404, {"error": "not found"}
            except Busy as busy:
                code, response = 409, {"error": str(busy)}
            except (ValueError, AttributeError) as bad:
                error, code, response = str(bad), 400, {"error": "invalid request"}
            except Exception as failure:
                error, response = str(failure), {"error": "server error"}
                logger.exception("POST failed")
            finally:
                log_post(self.path, request, response, error)
                self._json(code, response)

    return Handler


def create_server(studio: Studio, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """A server on localhost. Pass port 0 to take any free port."""
    server = ThreadingHTTPServer(("127.0.0.1", port), None)
    actual = server.server_address[1]
    server.RequestHandlerClass = make_handler(studio, {f"127.0.0.1:{actual}", f"localhost:{actual}"})
    return server


def serve(workspace: Path, port: int = DEFAULT_PORT, printer: str = "x1c") -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    studio = Studio(workspace, printer)
    server = create_server(studio, port)
    threading.Thread(target=studio.build_initial, daemon=True).start()
    print(f"cadsmith studio: http://127.0.0.1:{server.server_address[1]}  (workspace {studio.workspace})", flush=True)
    server.serve_forever()
