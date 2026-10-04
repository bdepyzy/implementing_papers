"""Plain-file store: every document is a folder $PAPER_INDEX_HOME/docs/<name>/ and the folder name is the key.

    meta.json  content.html | source.pdf  text.txt  annotations.json  notes.md  vectors.npz  archive/
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

HOME = Path(os.environ.get("PAPER_INDEX_HOME", "~/.paper_index")).expanduser()
PORT = int(os.environ.get("PAPER_INDEX_PORT", 8765))
DOCS = HOME / "docs"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
DEFAULT_SETTINGS = {"default": "yellow", "categories": [  # what each highlight color means
    {"id": "yellow", "label": "Key idea", "color": "#ffd628"}, {"id": "green", "label": "Agree / useful", "color": "#4cc96a"},
    {"id": "blue", "label": "Definition / reference", "color": "#5aa0ff"}, {"id": "pink", "label": "Question / doubt", "color": "#ff6fae"}]}
LOCK = threading.RLock()  # the server handles requests in threads; read-modify-write of files must not interleave
DOCS.mkdir(parents=True, exist_ok=True)


class NotFound(KeyError):
    def __str__(self) -> str:
        return f"not found: {self.args[0]}"


def read(path: Path, default=None):
    """JSON files are parsed, other files returned as text; `default` if missing."""
    if not path.exists():
        return default
    return json.loads(path.read_text("utf-8")) if path.suffix == ".json" else path.read_text("utf-8")


def write(path: Path, data) -> None:
    """Atomic (never a half-written file). Dicts and lists are stored as JSON."""
    data = json.dumps(data, indent=2, ensure_ascii=False) if isinstance(data, (dict, list)) else data
    (tmp := path.with_name(path.name + ".tmp")).write_bytes(data.encode() if isinstance(data, str) else data)
    os.replace(tmp, path)


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()[:60].rstrip("-") or "untitled"


# ---------- documents ----------

def iter_docs():
    return (p for p in sorted(DOCS.iterdir()) if (p / "meta.json").is_file())


def meta(p: Path) -> dict:
    return read(p / "meta.json", {}) | {"name": p.name, "n_annotations": len(read(p / "annotations.json", []))}


def save_meta(p: Path, m: dict) -> None:
    write(p / "meta.json", {k: v for k, v in m.items() if k not in ("name", "n_annotations")} | {"updated_at": time.time()})


def resolve(key: str) -> Path:
    """By name (exact or slugified), URL, title, or an unambiguous partial name."""
    for cand in (key, slugify(key)):
        if NAME_RE.fullmatch(cand) and (DOCS / cand / "meta.json").is_file():
            return DOCS / cand
    docs = [(p, meta(p)) for p in iter_docs()]
    exact = [p for p, m in docs if key in (m.get("url"), m["title"]) or key.lower() == m["title"].lower()]
    partial = [p for p, _ in docs if slugify(key) in p.name]
    if exact or len(partial) == 1:
        return (exact or partial)[0]
    raise NotFound(key)


def find_by_url(url: str) -> Path | None:
    """The doc saved from this page; a #fragment or trailing slash doesn't make it a different page."""
    same = lambda u: (u or "").split("#")[0].rstrip("/")
    return next((p for p in iter_docs() if same(meta(p).get("url")) == same(url)), None)


def new_name(name: str | None, title: str) -> str:
    if name and (DOCS / slugify(name)).exists():
        raise ValueError(f"name '{slugify(name)}' is already taken")
    base = slugify(name or title)
    return next(n for n in (base if i == 1 else f"{base}-{i}" for i in range(1, 10_000)) if not (DOCS / n).exists())


def add_doc(*, title: str, kind: str, text: str, content: str | bytes, url: str | None = None, name: str | None = None) -> dict:
    with LOCK:
        (p := DOCS / new_name(name, title)).mkdir()
    set_files(p, kind, text, content)
    write(p / "annotations.json", [])
    host, now = urlparse(url).hostname if url else None, time.time()
    write(p / "meta.json", {"title": title, "url": url, "site": host and host.removeprefix("www."), "kind": kind,
                            "created_at": now, "updated_at": now})
    return meta(p)


def set_files(p: Path, kind: str, text: str, content: str | bytes) -> None:
    write(p / ("source.pdf" if kind == "pdf" else "content.html"), content)
    write(p / "text.txt", text)


def get_doc(key: str) -> dict:
    p = resolve(key)
    return meta(p) | {"annotations": read(p / "annotations.json", []), "notes": read(p / "notes.md", "")}


def list_docs() -> list[dict]:
    return sorted((meta(p) for p in iter_docs()), key=lambda d: -d["created_at"])


def rename(key: str, name: str | None = None, title: str | None = None) -> dict:
    with LOCK:
        p = resolve(key)
        if title:
            save_meta(p, meta(p) | {"title": title})
        if name and slugify(name) != p.name:
            p = p.rename(DOCS / new_name(name, ""))
        return meta(p)


def delete(key: str) -> None:
    shutil.rmtree(resolve(key))


def here(tab: str, claim: bool = False, hash: str | None = None) -> dict:
    """The one live Paper Index tab and where it is. A tab claims it when opened; others then step aside."""
    with LOCK:
        s = read(HOME / "session.json", {"tab": None, "hash": ""})
        if claim or (hash is not None and s["tab"] == tab):
            write(HOME / "session.json", s := s | ({"tab": tab} if claim else {}) | ({"hash": hash} if hash is not None else {}))
        return s


def save_notes(key: str, notes: str) -> None:
    write(resolve(key) / "notes.md", notes)


# ---------- settings: what each highlight color means ----------

def load_settings() -> dict:
    try:
        return save_settings(read(HOME / "settings.json") or DEFAULT_SETTINGS, store=False)
    except ValueError:
        return DEFAULT_SETTINGS


def save_settings(s: dict, store: bool = True) -> dict:
    """Each default color keeps its id; its label and color, and which one is the default, can change."""
    given = {str(c.get("id")): c for c in s.get("categories", [])}
    cats = [c | {k: str(given[c["id"]].get(k) or c[k]).strip()[:40] for k in ("label", "color") if c["id"] in given} for c in DEFAULT_SETTINGS["categories"]]
    if not all(re.fullmatch(r"#[0-9a-fA-F]{6}", c["color"]) for c in cats):
        raise ValueError("colors must be #rrggbb")
    s = {"categories": cats, "default": s.get("default") if s.get("default") in {c["id"] for c in cats} else DEFAULT_SETTINGS["default"]}
    if store:
        write(HOME / "settings.json", s)
    return s


# ---------- annotations: highlights with a note, replies and a resolved state ----------

def edit_annotations(key: str, change, ann_id: int | None = None):
    """Run change(annotations, the annotation with ann_id) under the lock, save, and return its result."""
    with LOCK:
        p = resolve(key)
        anns = read(p / "annotations.json", [])
        target = next((a for a in anns if a["id"] == ann_id), None)
        if ann_id is not None and target is None:
            raise NotFound(f"highlight {ann_id}")
        result = change(anns, target)
        write(p / "annotations.json", anns)
        return result


def highlight(key: str, *, start: int, end: int, quote: str, page: int | None = None, view: str | None = None,
              comment: str = "", color: str | None = None) -> dict:
    if not quote.strip() or end <= start:
        raise ValueError("empty highlight")
    s, now = load_settings(), time.time()
    fields = {"page": page, "view": view, "start": int(start), "end": int(end), "quote": quote, "comment": comment, "created_at": now,
              "updated_at": now, "color": color if color in {c["id"] for c in s["categories"]} else s["default"]}

    def change(anns, _):
        anns.append(a := {"id": max((x["id"] for x in anns), default=0) + 1} | fields)
        return a
    return edit_annotations(key, change)


def edit_highlight(key: str, ann_id: int, comment=None, color=None, resolved=None, reply=None) -> dict:
    def change(_, a):
        a.update({k: v for k, v in (("comment", comment), ("color", color), ("resolved", resolved)) if v is not None}, updated_at=time.time())
        if reply and reply.strip():
            a.setdefault("replies", []).append({"text": reply.strip(), "created_at": time.time()})
        return a
    return edit_annotations(key, change, ann_id)


def remove_highlight(key: str, ann_id: int) -> None:
    edit_annotations(key, lambda anns, a: anns.remove(a), ann_id)
