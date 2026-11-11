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
from cadsmith.web import pipeline as pipeline_module
from cadsmith.web import server as server_module
from cadsmith.web.agent import ClaudeAgent
from cadsmith.web.pipeline import build_version
from cadsmith.web.server import Projects, Studio, create_server, describe_problems, log_post

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
        projects = Projects(tmp_path / "root", agent_command=fake_agent(tmp_path, script))
        studio = projects.create("project")
        if model:
            studio.model.write_text(model)
        server = create_server(projects, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return studio, f"http://127.0.0.1:{server.server_address[1]}/p/{studio.id}"

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
    assert kinds(studio) == ["user", "tool", "text", "build", "note", "tool", "text", "done"]
    state = json.loads(get(f"{url}/api/state")[1])
    assert state["report"]["version"] == 1 and state["report"]["passed"]
    code, body = get(f"{url}/files/v1/block.stl")
    assert code == 200 and len(body) > 84
    request = urllib.request.Request(f"{url}/files/v1/drawing.block.svg")
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.headers["Content-Type"] == "image/svg+xml"
        assert "sandbox" in response.headers["Content-Security-Policy"]
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
if n == 2:
    pathlib.Path("prompts.txt").write_text(prompt)
pathlib.Path("model.py").write_text({THIN!r} if n == 1 else {GOOD!r})
say(type="result", is_error=False)
"""
    studio, url = running(script)
    post(f"{url}/api/chat", {"message": "a cup"})
    wait_idle(studio)
    assert kinds(studio) == ["user", "build", "note", "build", "note", "done"]
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


SLOW_LOGGER = """log = pathlib.Path("prompts.txt")
log.write_text((log.read_text() if log.exists() else "") + prompt + "\\n=====\\n")
if "passed the automatic checks" not in prompt:
    time.sleep(2)
    pathlib.Path("model.py").write_text(GOOD)
say(type="result", is_error=False)
""".replace("GOOD", repr(GOOD))


def test_a_message_sent_while_working_reaches_the_next_step(running):
    studio, url = running(SLOW_LOGGER)
    assert post(f"{url}/api/chat", {"message": "one"}) == (202, {"ok": True, "queued": False})
    assert post(f"{url}/api/chat", {"message": "make it two"}) == (202, {"ok": True, "queued": True})
    assert json.loads(get(f"{url}/api/state")[1])["queued"] == 1
    wait_idle(studio, 120)
    steps = (studio.workspace / "prompts.txt").read_text().split("=====")
    assert "make it two" not in steps[0] and "make it two" in steps[1]
    assert "added this while you were working" in steps[1]
    assert not studio.pending


def test_a_message_after_the_last_step_starts_another_turn(running):
    studio, url = running('log = pathlib.Path("prompts.txt")\nlog.write_text((log.read_text() if log.exists() else "") + prompt + "|")\ntime.sleep(1)\nsay(type="result", is_error=False)\n', GOOD)
    post(f"{url}/api/chat", {"message": "a question"})
    post(f"{url}/api/chat", {"message": "and another"})
    wait_idle(studio, 60)
    assert (studio.workspace / "prompts.txt").read_text().split("|")[:2] == ["a question", "and another"]


def test_stop_drops_queued_messages(running):
    studio, url = running('say(type="system", session_id="s")\ntime.sleep(60)\n', GOOD)
    post(f"{url}/api/chat", {"message": "slow"})
    post(f"{url}/api/chat", {"message": "later"})
    post(f"{url}/api/stop", {})
    wait_idle(studio)
    assert not studio.pending and any("Dropped 1 queued message" in e["text"] for e in studio.events)


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


def root_of(url):
    return url.rsplit("/p/", 1)[0]


def names(url):
    return [item["name"] for item in json.loads(get(f"{root_of(url)}/api/projects")[1])["projects"]]


def test_home_opens_the_newest_project(running):
    studio, url = running()
    request = urllib.request.Request(f"{root_of(url)}/")
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.geturl().endswith(f"/p/{studio.id}/")


def test_new_project_starts_with_its_own_model(running):
    studio, url = running()
    code, created = post(f"{root_of(url)}/api/projects", {"name": "  Giá   đỡ "})
    assert code == 201 and created["name"] == "Giá đỡ"
    assert sorted(names(url)) == sorted(["project", "Giá đỡ"])
    state = json.loads(get(f"{root_of(url)}/p/{created['id']}/api/state")[1])
    assert state["name"] == "Giá đỡ"
    assert state["empty"] and state["report"] is None and state["status"] == "idle"


@pytest.mark.parametrize("name", ["", "   ", "x" * 61, "project", "PROJECT"])
def test_bad_or_duplicate_names_are_refused(running, name):
    _, url = running()
    code, body = post(f"{root_of(url)}/api/projects", {"name": name})
    assert code == 400 and body["error"]


def test_rename_keeps_folder_history_and_conversation(running):
    studio, url = running()
    post(f"{url}/api/chat", {"message": "go"})
    wait_idle(studio)
    folder, session = studio.workspace, studio.agent.session_id
    assert post(f"{url}/api/rename", {"name": "Hộp bút"}) == (200, {"id": studio.id, "name": "Hộp bút"})
    assert names(url) == ["Hộp bút"]
    assert studio.workspace == folder and studio.agent.session_id == session
    time.sleep(0.3)
    wait_idle(studio)
    assert studio.report["project"] == "Hộp bút"


def test_rename_to_its_own_name_with_new_case_is_allowed(running):
    _, url = running()
    assert post(f"{url}/api/rename", {"name": "Project"})[0] == 200


def test_rename_refuses_another_projects_name(running):
    _, url = running()
    post(f"{root_of(url)}/api/projects", {"name": "other"})
    assert post(f"{url}/api/rename", {"name": "Other"})[0] == 400


def test_delete_moves_to_trash_and_forgets_the_project(running):
    studio, url = running()
    folder = studio.workspace
    assert post(f"{url}/api/delete", {})[0] == 200
    assert not folder.exists()
    assert (folder.parent / ".trash" / studio.id / "project.json").exists()
    assert names(url) == []
    assert get(f"{url}/api/state")[0] == 404


def test_delete_refuses_a_working_project(running):
    studio, url = running('time.sleep(5)\nsay(type="result", is_error=False)\n', GOOD)
    post(f"{url}/api/chat", {"message": "slow"})
    assert post(f"{url}/api/delete", {})[0] == 409
    assert studio.workspace.exists()
    post(f"{url}/api/stop", {})
    wait_idle(studio)


def test_home_creates_a_project_after_the_last_is_deleted(running):
    studio, url = running()
    post(f"{url}/api/delete", {})
    code, body = get(f"{root_of(url)}/")
    assert code == 200 and b"cadsmith studio" in body
    assert names(url) == ["My first project"]


def test_unknown_project_is_not_found(running):
    _, url = running()
    assert get(f"{root_of(url)}/p/0000000000/api/state")[0] == 404
    assert post(f"{root_of(url)}/p/0000000000/api/chat", {"message": "x"})[0] == 404


def test_two_projects_work_independently(running):
    studio, url = running('time.sleep(3)\nsay(type="result", is_error=False)\n', GOOD)
    _, created = post(f"{root_of(url)}/api/projects", {"name": "other"})
    other = f"{root_of(url)}/p/{created['id']}"
    assert post(f"{url}/api/chat", {"message": "one"})[0] == 202
    time.sleep(0.2)
    end = time.time() + 60
    while time.time() < end and json.loads(get(f"{other}/api/state")[1])["status"] != "idle":
        time.sleep(0.2)
    assert post(f"{other}/api/chat", {"message": "two"})[0] == 202
    post(f"{url}/api/stop", {})
    post(f"{other}/api/stop", {})
    wait_idle(studio)


def test_old_single_workspace_becomes_a_project(tmp_path):
    root = tmp_path / "root"
    (root / "builds" / "v1").mkdir(parents=True)
    (root / "model.py").write_text(GOOD)
    (root / "session.json").write_text('{"session_id": "gone"}')
    (root / "builds" / "v1" / "report.json").write_text(json.dumps({"ok": True, "version": 1, "passed": True}))
    projects = Projects(root, agent_command=["true"])
    studio = next(iter(projects.studios.values()))
    assert studio.name == "My first project"
    assert studio.model.read_text() == GOOD
    assert studio.report["version"] == 1
    assert studio.agent.session_id is None
    assert not (root / "model.py").exists()


def test_projects_survive_a_restart(tmp_path):
    first = Projects(tmp_path / "root", agent_command=["true"]).create("keep me")
    again = Projects(tmp_path / "root", agent_command=["true"])
    assert again.get(first.id).name == "keep me"


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
    assert calls[-1][0].endswith("/api/chat")
    assert calls[-1][2]["error"].startswith("send a message")


def test_empty_project_has_nothing_to_build(tmp_path):
    (tmp_path / "fresh").mkdir()
    studio = Studio(tmp_path / "fresh", agent_command=["true"])
    studio.build_initial()
    studio.rebuild()
    assert studio.is_empty and studio.report is None
    assert studio.events == [] and studio.status == "idle"
    assert studio._versions() == []


def test_first_message_creates_the_model(running):
    studio, url = running()
    assert studio.is_empty
    post(f"{url}/api/chat", {"message": "make a block"})
    wait_idle(studio)
    assert not studio.is_empty
    assert studio.report["version"] == 1


def test_a_question_in_an_empty_project_stays_empty(running):
    studio, url = running('say(type="result", is_error=False)\n')
    post(f"{url}/api/chat", {"message": "what can you make?"})
    wait_idle(studio)
    assert studio.is_empty and "build" not in kinds(studio)


def test_old_placeholder_projects_become_empty(tmp_path):
    project = tmp_path / "old"
    (project / "builds" / "v1").mkdir(parents=True)
    (project / "model.py").write_text(server_module.OLD_STARTER_MODEL)
    (project / "events.jsonl").write_text(json.dumps({"seq": 1, "kind": "build", "text": "x"}) + "\n")
    studio = Studio(project, agent_command=["true"])
    assert studio.is_empty and studio.report is None and studio.events == []
    assert studio._versions() == []


def test_a_real_model_is_never_treated_as_the_placeholder(tmp_path):
    project = tmp_path / "real"
    project.mkdir()
    (project / "model.py").write_text(server_module.OLD_STARTER_MODEL + "# edited\n")
    assert not Studio(project, agent_command=["true"]).is_empty


REVIEWS_THEN_STOPS = """say(type="system", session_id="s1")
log = pathlib.Path("prompts.txt")
log.write_text((log.read_text() if log.exists() else "") + prompt + "\\n=====\\n")
if "passed the automatic checks" not in prompt:
    pathlib.Path("model.py").write_text(GOOD)
say(type="result", is_error=False)
""".replace("GOOD", repr(GOOD))


KEEPS_CHANGING = """say(type="system", session_id="s1")
pathlib.Path("model.py").write_text(GOOD + "# " + str(time.time()) + "\\n")
say(type="result", is_error=False)
""".replace("GOOD", repr(GOOD))


def test_a_clean_build_is_reviewed_and_an_unchanged_model_ends_the_turn(running):
    studio, url = running(REVIEWS_THEN_STOPS)
    post(f"{url}/api/chat", {"message": "a block please"})
    wait_idle(studio)
    prompts = (studio.workspace / "prompts.txt").read_text()
    assert "builds/v1/preview.png" in prompts and "a block please" in prompts
    assert (studio.builds / "v1" / "preview.png").stat().st_size > 1000
    assert studio.report["version"] == 1
    assert kinds(studio).count("build") == 1 and kinds(studio)[-1] == "done"


def test_review_prompt_lists_what_would_hurt_the_print():
    check = {"warnings": ["needs support under the lid"], "errors": []}
    report = {"version": 2, "preview": "preview.png", "size": [1, 2, 3], "parts": [{"name": "lid", "check": check}]}
    prompt = server_module.review_prompt(report, "a box")
    assert "lid: needs support under the lid" in prompt and "count as defects" in prompt


def test_a_failed_fit_says_where():
    report = {"ok": True, "parts": [], "plate_error": None,
              "fits": [{"passed": False, "message": "a / b: too tight", "location": [1.0, 2.0, 3.0]}]}
    assert "a / b: too tight near (1.0, 2.0, 3.0)" in describe_problems(report)


def test_reviews_are_bounded(running):
    studio, url = running(KEEPS_CHANGING)
    post(f"{url}/api/chat", {"message": "go"})
    wait_idle(studio)
    assert kinds(studio).count("build") == server_module.MAX_REVIEWS + 1
    assert kinds(studio)[-1] == "done"


def test_a_rebuild_does_not_call_the_agent(running):
    studio, _ = running(REVIEWS_THEN_STOPS, model=GOOD)
    studio.rebuild()
    wait_idle(studio)
    assert studio.report["passed"] and not (studio.workspace / "prompts.txt").exists()


def test_a_failed_preview_does_not_fail_the_build(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("no picture")
    monkeypatch.setattr(pipeline_module, "render_preview", broken)
    (tmp_path / "model.py").write_text(GOOD)
    report = build_version(tmp_path, 1, "x1c")
    assert report["passed"] and report["preview"] is None


def test_build_writes_drawing_sheets(tmp_path):
    (tmp_path / "model.py").write_text(WITH_BOUGHT)
    report = build_version(tmp_path, 1, "x1c")
    assert report["drawings"] == [{"name": "assembly", "file": "drawing.svg"},
                                  {"name": "plate", "file": "drawing.plate.svg"}]
    assert (tmp_path / "builds" / "v1" / "drawing.plate.svg").read_text().startswith("<svg")


def test_a_failed_drawing_does_not_fail_the_build(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("no sheet")
    monkeypatch.setattr(pipeline_module, "part_sheet", broken)
    (tmp_path / "model.py").write_text(GOOD)
    report = build_version(tmp_path, 1, "x1c")
    assert report["passed"] and report["drawings"] == []


def test_report_gives_the_size(tmp_path):
    (tmp_path / "model.py").write_text("import cadquery as cq\nresult = cq.Workplane('XY').box(30, 20, 10)\n")
    report = build_version(tmp_path, 1, "x1c")
    assert report["size"] == [30, 20, 10] and report["parts"][0]["size"] == [30, 20, 10]


WITH_BOUGHT = """import cadquery as cq
BOUGHT = {"motor"}
def parts():
    return {"plate": cq.Workplane("XY").box(40, 40, 4), "motor": cq.Workplane("XY").workplane(offset=2).box(10, 10, 10, centered=(True, True, False))}
"""


def test_bought_parts_are_shown_but_not_printed(tmp_path):
    (tmp_path / "model.py").write_text(WITH_BOUGHT)
    report = build_version(tmp_path, 1, "x1c")
    assert report["passed"]
    motor = next(part for part in report["parts"] if part["name"] == "motor")
    assert motor["bought"] and motor["check"]["passed"]
    assert (tmp_path / "builds" / "v1" / "motor.stl").exists()
    assert [p["name"] for plate in report["plates"] for p in plate["parts"]] == ["plate"]


def test_prompt_carries_the_guide_and_real_helper_signatures():
    prompt = agent_module.system_prompt()
    assert "Object or machine" in prompt
    assert "servo_cutout(kind: str='SG90'" in prompt and "soften_edges(" in prompt
    assert "SERVO_TABLE" in prompt
    assert "notes the user pinned on the model" in prompt


def upload(url, name, data):
    request = urllib.request.Request(f"{url}/api/upload", data=data, method="POST",
                                     headers={"Content-Type": "application/octet-stream", "X-Filename": name})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40


def test_upload_a_picture_and_serve_it_back(running):
    studio, url = running()
    code, body = upload(url, "my%20shot.png", PNG)
    assert code == 201 and body["path"] == "uploads/my-shot.png" and body["kind"] == "image"
    assert upload(url, "my%20shot.png", PNG)[1]["path"] == "uploads/my-shot-2.png"
    code, data = get(f"{url}/uploads/my-shot.png")
    assert code == 200 and data == PNG


def test_upload_an_stl_reports_its_size(running, tmp_path):
    studio, url = running()
    path = tmp_path / "b.stl"
    import cadquery as cq
    cq.Workplane().box(10, 20, 30).val().exportStl(str(path))
    code, body = upload(url, "b.stl", path.read_bytes())
    assert code == 201 and body["kind"] == "mesh" and body["size"] == [10, 20, 30] and body["open_edges"] == 0
    assert (studio.workspace / "uploads" / "b.stl").is_file()


def test_upload_rejects_wrong_files(running):
    _, url = running()
    assert upload(url, "notes.txt", b"hello")[0] == 400
    assert upload(url, "fake.png", b"not a png at all")[0] == 400
    assert upload(url, "junk.stl", b"junk")[0] == 400
    assert get(f"{url}/uploads/../session.json")[0] == 404


def test_prompt_explains_attachments():
    assert "import_stl" in agent_module.system_prompt() and "Never Read an STL" in agent_module.system_prompt()


def test_studio_page_has_the_note_tool(running):
    _, url = running()
    page = get(f"{url}/")[1].decode()
    assert 'id="notetool"' in page and "Notes pinned on the model" in page


def test_a_turn_that_runs_out_of_time_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "AGENT_TIMEOUT", 0.3)
    agent = ClaudeAgent(tmp_path, fake_agent(tmp_path, "time.sleep(20)\n"))
    result = agent.run("hello", lambda *a: None)
    assert not result.ok and "ran out of time" in result.error


def test_agent_module_describes_the_contract():
    assert "parts()" in agent_module.SYSTEM_PROMPT
