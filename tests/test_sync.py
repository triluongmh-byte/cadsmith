import json
import shutil
import subprocess
import time

import pytest

from cadsmith.web.server import Projects, Studio
from cadsmith.web.sync import GitSync, SyncError
from test_web import GOOD, fake_agent

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(path, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def two_computers(tmp_path):
    """A shared remote and two clones of it, as on two computers."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(remote), str(seed))
    (seed / "README.md").write_text("projects\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "start")
    git(seed, "push", "-q", "-u", "origin", "main")
    clones = []
    for name in ("one", "two"):
        git(tmp_path, "clone", "-q", str(remote), str(tmp_path / name))
        clones.append(tmp_path / name)
    return clones


def write_project(root, project_id, name, model=GOOD, events=()):
    folder = root / project_id
    (folder / "builds" / "v1").mkdir(parents=True)
    (folder / "builds" / "v1" / "big.stl").write_text("solid")
    (folder / "project.json").write_text(json.dumps({"name": name}))
    (folder / "model.py").write_text(model)
    (folder / "session.json").write_text(json.dumps({"session_id": "local"}))
    with (folder / "events.jsonl").open("a") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
    return folder


def test_a_folder_that_is_not_a_repository_does_not_sync(tmp_path):
    sync = GitSync(tmp_path)
    assert not sync.enabled and sync.status() == {"enabled": False}
    assert not (tmp_path / ".gitignore").exists()


def test_a_project_reaches_the_other_computer_without_its_builds(two_computers):
    one, two = two_computers
    write_project(one, "a1b2c3d4e5", "Pen box")
    GitSync(one).save("Pen box: v1")
    assert GitSync(two).sync() is True
    assert (two / "a1b2c3d4e5" / "model.py").read_text() == GOOD
    assert not (two / "a1b2c3d4e5" / "builds").exists()
    assert not (two / "a1b2c3d4e5" / "session.json").exists()
    assert GitSync(one).status()["unpushed"] == 0


def test_both_computers_can_add_to_one_conversation(two_computers):
    one, two = two_computers
    write_project(one, "a1b2c3d4e5", "Pen box", events=[{"seq": 1, "kind": "user", "text": "start"}])
    GitSync(one).save("start")
    GitSync(two).sync()
    with (one / "a1b2c3d4e5" / "events.jsonl").open("a") as handle:
        handle.write(json.dumps({"seq": 2, "kind": "user", "text": "from one"}) + "\n")
    with (two / "a1b2c3d4e5" / "events.jsonl").open("a") as handle:
        handle.write(json.dumps({"seq": 2, "kind": "user", "text": "from two"}) + "\n")
    GitSync(one).save("one")
    GitSync(two).sync()
    texts = [json.loads(line)["text"] for line in (two / "a1b2c3d4e5" / "events.jsonl").read_text().splitlines()]
    assert texts[0] == "start" and set(texts[1:]) == {"from one", "from two"}


def test_a_model_changed_on_both_computers_is_left_alone(two_computers):
    one, two = two_computers
    write_project(one, "a1b2c3d4e5", "Pen box")
    GitSync(one).save("start")
    GitSync(two).sync()
    (one / "a1b2c3d4e5" / "model.py").write_text(GOOD + "# one\n")
    GitSync(one).save("one")
    (two / "a1b2c3d4e5" / "model.py").write_text(GOOD + "# two\n")
    sync = GitSync(two)
    with pytest.raises(SyncError, match="same file"):
        sync.sync()
    assert (two / "a1b2c3d4e5" / "model.py").read_text() == GOOD + "# two\n"
    assert not (two / ".git" / "rebase-merge").exists() and not (two / ".git" / "rebase-apply").exists()
    assert sync.status()["error"]


def test_a_failed_push_is_kept_as_an_error_and_retried(two_computers, tmp_path):
    one, _ = two_computers
    git(one, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    write_project(one, "a1b2c3d4e5", "Pen box")
    sync = GitSync(one)
    sync.save("start")  # does not raise
    assert sync.error
    git(one, "remote", "set-url", "origin", str(tmp_path / "remote.git"))
    sync.save("nothing new")
    assert sync.error is None and sync.status()["unpushed"] == 0


def test_the_studio_syncs_on_start_and_opens_what_came_in(two_computers):
    one, two = two_computers
    write_project(one, "a1b2c3d4e5", "Pen box")
    GitSync(one).save("start")
    projects = Projects(two)
    assert [item["name"] for item in projects.list()] == ["Pen box"]
    write_project(one, "f0e1d2c3b4", "Lid")
    GitSync(one).save("lid")
    assert projects.sync()["changed"] is True
    assert sorted(item["name"] for item in projects.list()) == ["Lid", "Pen box"]


def test_an_agent_without_its_conversation_gets_a_recap(tmp_path):
    folder = write_project(tmp_path, "a1b2c3d4e5", "Pen box", events=[
        {"seq": 1, "kind": "user", "text": "make a pen box 80 mm long"},
        {"seq": 2, "kind": "text", "text": "Made it with a 2 mm wall."}])
    (folder / "session.json").unlink()
    command = fake_agent(tmp_path, 'pathlib.Path("prompt.txt").write_text(prompt, encoding="utf-8")\nsay(type="result", is_error=False)\n')
    studio = Studio(folder, agent_command=command)
    studio.send("how long is it?")
    while studio.status != "idle":
        time.sleep(0.05)
    prompt = (folder / "prompt.txt").read_text(encoding="utf-8")
    assert "make a pen box 80 mm long" in prompt and "Made it with a 2 mm wall." in prompt
    assert prompt.endswith("how long is it?") and prompt.count("how long is it?") == 1
