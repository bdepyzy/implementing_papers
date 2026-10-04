"""If the library folder is inside a git repo, it's an archive: a minute after your last change, each changed
article is committed as `saved "<title>"` (or `removed "<name>"`) and pushed. Saves from your other machines are
pulled when the server starts, every few minutes, and before each push."""
from __future__ import annotations

import subprocess
import threading
from pathlib import PurePosixPath

from . import store

QUIET_SECONDS = 60
PULL_MINUTES = 10
IGNORE = "docs/*/archive/\ndocs/*/vectors*.npz\nsession.json\n*.tmp\n"  # caches: big, and rebuilt on demand
_timer: threading.Timer | None = None
_lock, _git_lock = threading.Lock(), threading.Lock()  # _git_lock: commits and pulls never overlap


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(store.HOME), *args], capture_output=True, text=True)


def changed() -> None:
    """Call after every change; commits once things have been quiet for QUIET_SECONDS."""
    global _timer
    with _lock:
        if _timer:
            _timer.cancel()
        _timer = threading.Timer(QUIET_SECONDS, commit)
        _timer.daemon = True
        _timer.start()


def in_repo() -> bool:
    return git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def pull() -> None:
    """Bring in saves from your other machines. If they can't be combined cleanly, keep ours and try again later."""
    if git("pull", "--rebase", "--autostash", "-q").returncode:
        git("rebase", "--abort")


def keep_in_sync() -> None:
    """Pull now, and every PULL_MINUTES while the server runs; also push saves an earlier push didn't get out."""
    if in_repo():
        with _git_lock:
            pull()
            git("push", "-q")
    timer = threading.Timer(PULL_MINUTES * 60, keep_in_sync)
    timer.daemon = True
    timer.start()


def commit() -> None:
    with _git_lock:
        if in_repo():
            _commit()


def _commit() -> None:
    if not (store.HOME / ".gitignore").exists():
        store.write(store.HOME / ".gitignore", IGNORE)
    library = PurePosixPath(git("rev-parse", "--show-prefix").stdout.strip())  # the library's path inside the repo
    groups: dict[str, list[str]] = {}
    for entry in git("status", "--porcelain", "-z", "--untracked-files=all", "--", ".").stdout.split("\0"):
        if entry:
            parts = PurePosixPath(entry[3:]).relative_to(library).parts
            groups.setdefault(parts[1] if parts[0] == "docs" and len(parts) > 2 else "", []).append(str(PurePosixPath(*parts)))
    for name, paths in groups.items():
        title = store.read(store.DOCS / name / "meta.json", {}).get("title") if name else None
        message = f'saved "{title}"' if title else f'removed "{name}"' if name else "saved settings"
        git("add", "-A", "--", *paths)
        git("commit", "-q", "-m", message, "--", *paths)
    if groups:
        pull()  # another machine may have pushed meanwhile
        git("push", "-q")
