"""If the library folder is inside a git repo, it's an archive. Every COMMIT_MINUTES (and when the server starts),
each changed article is committed as `saved "<title>"` (or `removed "<name>"`) and pushed. Saves from your other
machines are pulled every PULL_MINUTES."""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import PurePosixPath

from . import store

COMMIT_MINUTES = 120
PULL_MINUTES = 10
IGNORE = "docs/*/archive/\ndocs/*/vectors*.npz\nsession.json\n*.tmp\n"  # caches: big, and rebuilt on demand
_lock = threading.Lock()  # git operations never overlap
_last_commit = 0.0


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(store.HOME), *args], capture_output=True, text=True)


def keep_in_sync() -> None:
    """Commit if it's time (always on the first run), pull, push; then again in PULL_MINUTES."""
    global _last_commit
    with _lock:
        if git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true":
            if time.time() - _last_commit >= COMMIT_MINUTES * 60:
                commit()
                _last_commit = time.time()
            if git("pull", "--rebase", "--autostash", "-q").returncode:  # can't combine cleanly: keep ours, retry later
                git("rebase", "--abort")
            git("push", "-q")
    timer = threading.Timer(PULL_MINUTES * 60, keep_in_sync)
    timer.daemon = True
    timer.start()


def commit() -> None:
    """One commit per changed article; other changes (settings, the .gitignore) get their own."""
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
        message = f'saved "{title}"' if title else f'removed "{name}"' if name else \
            "saved settings" if "settings.json" in paths else "set up library"
        git("add", "-A", "--", *paths)
        git("commit", "-q", "-m", message, "--", *paths)
