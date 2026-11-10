import json
import socket
import stat
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from cadsmith.web import agent as agent_module
from cadsmith.web import server as server_module
from cadsmith.web.agent import ClaudeAgent
from cadsmith.web.pipeline import build_version
from cadsmith.web.server import Studio, create_server, describe_problems, log_post

GOOD = "import cadquery as cq\ndef parts():\n    return {'block': cq.Workplane('XY').box(30, 20, 10)}\n"
THIN = "import cadquery as cq\ndef parts():\n    return {'cup': cq.Workplane('XY').box(30, 30, 20).faces('>Z').shell(-0.4)}\n"


def fake_agent(tmp_path, script: str) -> list[str]:
    """An executable that stands in for the claude command line."""
    path = tmp_path / "fake_claude.py"
    path.write_text(f"#!{sys.executable}\nimport json, sys, pathlib, time\nprompt = sys.stdin.read()\n"
                    "def say(**kw):\n    print(json.dumps(kw), flush=True)\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return [str(path)]


WRITES_GOOD = f"""say(type="system", session_id="s1")
say(type="assistant", message={{"content": [{{"type": "tool_use", "name": "Write", "input": {{"file_path": "model.py"}}}}]}})
pathlib.Path("model.py").write_text({GOOD!r})
say(type="assistant", message={{"content": [{{"type": "text", "text": "Made a block."}}]}})
say(type="result", is_error=False)
"""


@pytest.fixture
def running(tmp_path):
    servers = []

    def start(script=WRITES_GOOD, model=None):
        workspace = tmp_path / "project"
        workspace.mkdir(exist_ok=True)
        if model:
            (workspace / "model.py").write_text(model)
        studio = Studio(workspace, agent_command=fake_agent(tmp_path, script))
        server = create_server(studio, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return studio, f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def post(url, body, headers=None):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def wait_idle(studio, limit=90):
    end = time.time() + limit
    while time.time() < end:
        if studio.status == "idle":
            return
        time.sleep(0.1)
    raise AssertionError("the turn did not finish")


def kinds(studio):
    return [event["kind"] for event in studio.events]


def test_pipeline_writes_files_and_report(tmp_path):
    (tmp_path / "model.py").write_text(GOOD)
    report = build_version(tmp_path, 1, "x1c")
    assert report["passed"]
    assert report["parts"][0]["name"] == "block"
    assert report["plates"][0]["parts"][0]["name"] == "block"
    out = tmp_path / "builds" / "v1"
    assert {path.name for path in out.iterdir()} >= {"block.stl", "block.step", "block.3mf", "plates.3mf", "model.py"}


def test_pipeline_records_a_part_too_big_for_any_plate(tmp_path):
    (tmp_path / "model.py").write_text("import cadquery as cq\nresult = cq.Workplane('XY').box(400, 20, 10)\n")
    report = build_version(tmp_path, 1, "x1c")
    assert not report["passed"]
    assert "does not fit" in report["plate_error"]


def test_describe_problems():
    assert "boom" in describe_problems({"ok": False, "error": "boom"})
    clean = {"ok": True, "parts": [{"name": "a", "check": {"errors": []}}], "fits": [], "plate_error": None}
    assert describe_problems(clean) == ""
    bad = {"ok": True, "parts": [{"name": "a", "check": {"errors": ["thin"]}}], "fits": [{"passed": False, "message": "tight"}],
           "plate_error": "big"}
    assert describe_problems(bad) == "a: thin\ntight\nbig"


def test_agent_is_restricted_to_editing_files(tmp_path):
    arguments = ClaudeAgent(tmp_path)._arguments()
    assert arguments[arguments.index("--allowedTools") + 1] == "Read,Edit,Write"
    assert "Bash" in arguments[arguments.index("--disallowedTools") + 1]
    assert "--resume" not in arguments
    assert "--resume" in ClaudeAgent(tmp_path, session_id="x")._arguments()


def test_tool_lines_use_paths_relative_to_the_workspace(tmp_path):
    seen = []
    line = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": f"{tmp_path}/model.py"}}]}})
    ClaudeAgent(tmp_path)._handle(line, lambda kind, text: seen.append((kind, text)))
    assert seen == [("tool", "Write model.py")]


def test_agent_reports_a_missing_command(tmp_path):
    result = ClaudeAgent(tmp_path, ["/no/such/claude"]).run("hi", lambda *_: None)
    assert not result.ok
    assert "Claude Code" in result.error


def test_agent_passes_a_failed_result_on(tmp_path):
    command = fake_agent(tmp_path, 'say(type="result", is_error=True, result="out of credits")\n')
    result = ClaudeAgent(tmp_path, command).run("hi", lambda *_: None)
    assert (result.ok, result.error) == (False, "out of credits")


def test_prompt_starting_with_a_dash_is_not_an_option(tmp_path):
    command = fake_agent(tmp_path, 'pathlib.Path("seen.txt").write_text(prompt + " | " + " ".join(sys.argv[1:]))\nsay(type="result", is_error=False)\n')
    ClaudeAgent(tmp_path, command).run("--dangerous", lambda *_: None)
    prompt, _, arguments = (tmp_path / "seen.txt").read_text().partition(" | ")
    assert prompt == "--dangerous"
    assert "--dangerous" not in arguments.split()


def test_chat_builds_a_new_version(running):
    studio, url = running()
    assert post(f"{url}/api/chat", {"message": "make a block"})[0] == 202
    wait_idle(studio)
    assert kinds(studio) == ["user", "tool", "text", "build", "done"]
    state = json.loads(get(f"{url}/api/state")[1])
    assert state["report"]["version"] == 1 and state["report"]["passed"]
    code, body = get(f"{url}/files/v1/block.stl")
    assert code == 200 and len(body) > 84
    assert studio.agent.session_id == "s1"


def test_state_returns_only_new_events(running):
    studio, url = running()
    post(f"{url}/api/chat", {"message": "go"})
    wait_idle(studio)
    total = len(studio.events)
    assert len(json.loads(get(f"{url}/api/state?since={total - 1}")[1])["events"]) == 1


def test_a_question_does_not_build(running):
    studio, url = running('say(type="assistant", message={"content": [{"type": "text", "text": "It is 60 mm."}]})\nsay(type="result", is_error=False)\n', GOOD)
    post(f"{url}/api/chat", {"message": "how long?"})
    wait_idle(studio)
    assert kinds(studio) == ["user", "text"]
    assert studio._versions() == []


def test_failed_checks_go_back_to_the_agent_until_fixed(running, tmp_path):
    script = f"""count = pathlib.Path("count.txt")
n = int(count.read_text()) + 1 if count.exists() else 1
count.write_text(str(n))
if n > 1:
    pathlib.Path("prompts.txt").write_text(prompt)
pathlib.Path("model.py").write_text({THIN!r} if n == 1 else {GOOD!r})
say(type="result", is_error=False)
"""
    studio, url = running(script)
    post(f"{url}/api/chat", {"message": "a cup"})
    wait_idle(studio)
    assert kinds(studio) == ["user", "build", "note", "build", "done"]
    assert "wall 0.4" in (studio.workspace / "prompts.txt").read_text()


def test_repairs_stop_after_the_limit(running):
    script = f'pathlib.Path("model.py").write_text({THIN!r} + "# " + str(time.time()))\nsay(type="result", is_error=False)\n'
    studio, url = running(script)
    post(f"{url}/api/chat", {"message": "a cup"})
    wait_idle(studio, 180)
    assert kinds(studio).count("build") == server_module.MAX_REPAIRS + 1
    assert kinds(studio)[-1] == "error"


def test_broken_model_keeps_the_last_good_preview(running):
    script = 'pathlib.Path("model.py").write_text("def parts(:\\n")\nsay(type="result", is_error=False)\n'
    studio, url = running(script, GOOD)
    studio.build()
    post(f"{url}/api/chat", {"message": "break it"})
    wait_idle(studio, 180)
    state = json.loads(get(f"{url}/api/state")[1])
    assert state["report"]["version"] == 1
    assert "SyntaxError" in state["error"]


def test_second_message_while_busy_is_refused(running):
    studio, url = running('time.sleep(4)\nsay(type="result", is_error=False)\n', GOOD)
    assert post(f"{url}/api/chat", {"message": "one"})[0] == 202
    assert post(f"{url}/api/chat", {"message": "two"})[0] == 409
    post(f"{url}/api/stop", {})
    wait_idle(studio)


def test_stop_ends_the_turn(running):
    studio, url = running('say(type="system", session_id="s")\ntime.sleep(60)\n', GOOD)
    post(f"{url}/api/chat", {"message": "slow"})
    deadline = time.time() + 10
    while studio.agent.session_id is None and time.time() < deadline:
        time.sleep(0.05)
    assert post(f"{url}/api/stop", {})[1] == {"stopped": True}
    wait_idle(studio, 10)
    assert studio.events[-1]["text"] == "Stopped."


@pytest.mark.parametrize("body", [{}, {"message": "   "}, {"message": "x" * 9000}])
def test_bad_messages_are_refused(running, body):
    _, url = running()
    assert post(f"{url}/api/chat", body)[0] == 400


def test_requests_from_other_sites_are_refused(running):
    _, url = running()
    assert post(f"{url}/api/chat", {"message": "x"}, {"Origin": "https://evil.example"})[0] == 403
    assert post(f"{url}/api/chat", {"message": "x"}, {"Host": "evil.example"})[0] == 403
    assert get(f"{url}/api/state", {"Host": "evil.example"})[0] == 403


def test_form_posts_are_refused(running):
    _, url = running()
    request = urllib.request.Request(f"{url}/api/chat", data=b"message=x", method="POST",
                                     headers={"Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    assert caught.value.code == 415


def test_only_build_files_are_served(running):
    studio, url = running(model=GOOD)
    (studio.builds / "v1").mkdir()
    (studio.builds / "v1" / "secret.txt").write_text("no")
    (studio.workspace / "session.json").write_text("{}")
    assert get(f"{url}/files/v1/secret.txt")[0] == 404
    assert get(f"{url}/files/v1/../../session.json")[0] == 404
    assert get(f"{url}/files/v1/%2e%2e/%2e%2e/session.json")[0] == 404


def test_page_is_served(running):
    _, url = running()
    code, body = get(url + "/")
    assert code == 200 and b"cadsmith studio" in body


def test_history_survives_a_restart(running, tmp_path):
    studio, url = running()
    post(f"{url}/api/chat", {"message": "go"})
    wait_idle(studio)
    again = Studio(studio.workspace, agent_command=["true"])
    assert again.report["version"] == 1
    assert [event["kind"] for event in again.events] == kinds(studio)
    assert again.agent.session_id == "s1"


def test_post_is_logged_to_graylog(monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(3)
        monkeypatch.setenv("CADSMITH_GRAYLOG", f"127.0.0.1:{receiver.getsockname()[1]}")
        log_post("/api/chat", {"message": "hello"}, {"ok": True}, "broke")
        message = json.loads(receiver.recv(9000))
    assert message["version"] == "1.1"
    assert message["_path"] == "/api/chat"
    assert "hello" in message["_request"] and "ok" in message["_response"] and message["_error"] == "broke"


def test_failed_requests_are_logged_too(running, monkeypatch):
    calls = []
    monkeypatch.setattr(server_module, "log_post", lambda *args: calls.append(args))
    _, url = running()
    post(f"{url}/api/chat", {"message": ""})
    assert calls[-1][0] == "/api/chat"
    assert calls[-1][2]["error"].startswith("send a message")


def test_starter_model_builds(tmp_path):
    (tmp_path / "fresh").mkdir()
    studio = Studio(tmp_path / "fresh", agent_command=["true"])
    studio.build_initial()
    assert studio.report["passed"]
    assert kinds(studio) == ["build", "done"]


def test_agent_module_describes_the_contract():
    assert "parts()" in agent_module.SYSTEM_PROMPT
