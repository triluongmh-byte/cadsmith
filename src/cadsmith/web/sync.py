"""Keep the projects folder in a git repository, so the projects follow you to another computer.

The folder syncs only when it is a git repository already (``git init`` or ``git clone`` it yourself).
Builds are left out: a computer that pulls a project builds it again from ``model.py``.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

GIT_TIMEOUT = 120
IGNORED = ("*/builds/", "*/session.json", ".trash/", "__pycache__/", "*.pyc")
# Both computers only ever append to a conversation, so a merge keeps the lines from each.
ATTRIBUTES = ("*/events.jsonl merge=union",)
FALLBACK_IDENTITY = ("-c", "user.name=cadsmith", "-c", "user.email=cadsmith@localhost")


class SyncError(Exception):
    pass


def _ensure_lines(path: Path, lines: tuple[str, ...]) -> None:
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    missing = [line for line in lines if line not in existing]
    if missing:
        text = "\n".join(existing + missing) + "\n"
        path.write_text(text, encoding="utf-8")


class GitSync:
    def __init__(self, root: Path):
        self.root = root
        self.enabled = (root / ".git").exists()
        self.error: str | None = None
        self._lock = threading.Lock()
        self._identity: tuple[str, ...] | None = None
        if self.enabled:
            _ensure_lines(root / ".gitignore", IGNORED)
            _ensure_lines(root / ".gitattributes", ATTRIBUTES)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        try:
            done = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise SyncError("git is not installed") from None
        except subprocess.TimeoutExpired:
            raise SyncError(f"git {args[0]} took over {GIT_TIMEOUT} s") from None
        if check and done.returncode:
            raise SyncError((done.stderr or done.stdout).strip()[-600:] or f"git {args[0]} failed")
        return done

    def _author(self) -> tuple[str, ...]:
        """Commit as the person when git knows them, so a fresh computer can still commit."""
        if self._identity is None:
            known = self._git("config", "user.email", check=False).stdout.strip()
            self._identity = () if known else FALLBACK_IDENTITY
        return self._identity

    def _has_upstream(self) -> bool:
        return self._git("rev-parse", "--abbrev-ref", "@{upstream}", check=False).returncode == 0

    def _has_remote(self) -> bool:
        return bool(self._git("remote", check=False).stdout.strip())

    def _commit(self, message: str) -> bool:
        self._git("add", "-A")
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self._git(*self._author(), "commit", "-q", "-m", message)
        return True

    def _pull(self) -> bool:
        """Bring in the other computer's work. Returns True when anything changed here."""
        if not self._has_upstream():
            return False
        before = self._git("rev-parse", "HEAD", check=False).stdout.strip()
        done = self._git(*self._author(), "pull", "--rebase", "--autostash", "-q", check=False)
        if done.returncode:
            self._git("rebase", "--abort", check=False)
            raise SyncError("both computers changed the same file, so nothing was merged:\n"
                            + (done.stderr or done.stdout).strip()[-600:])
        return self._git("rev-parse", "HEAD", check=False).stdout.strip() != before

    def _push(self) -> None:
        if self._has_upstream():
            self._git("push", "-q")
        elif self._has_remote():
            branch = self._git("branch", "--show-current").stdout.strip()
            self._git("push", "-q", "-u", "origin", branch)

    def _run(self, work):
        if not self.enabled:
            return None
        with self._lock:
            try:
                result = work()
            except SyncError as failure:
                self.error = str(failure)
                raise
            self.error = None
            return result

    def save(self, message: str) -> None:
        """Commit what changed and push it. Failing to push is kept as ``error``, never raised."""
        try:
            self._run(lambda: (self._commit(message), self._push()))  # also retries an earlier failed push
        except SyncError:
            pass

    def pull(self) -> bool:
        return bool(self._run(self._pull))

    def sync(self, message: str = "Sync") -> bool:
        """Commit, take the other computer's work, then push. Returns True when anything came in."""
        def work():
            self._commit(message)
            changed = self._pull()
            self._push()
            return changed
        return bool(self._run(work))

    def status(self) -> dict:
        if not self.enabled:
            return {"enabled": False}
        with self._lock:
            ahead = None
            if self._has_upstream():
                counted = self._git("rev-list", "--count", "@{upstream}..HEAD", check=False).stdout.strip()
                ahead = int(counted) if counted.isdigit() else None
            return {"enabled": True, "remote": self._has_remote(), "unpushed": ahead, "error": self.error}
