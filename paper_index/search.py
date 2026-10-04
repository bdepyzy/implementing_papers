"""Search: name/site lookup + keywords + meaning (model2vec static embeddings), fused by reciprocal rank."""
from __future__ import annotations

import hashlib
import math
import os
import re
import sys
import threading
from collections import Counter

import numpy as np

from . import store

MODEL_NAME = os.environ.get("PAPER_INDEX_MODEL", "minishlab/potion-base-8M")  # ~30MB, CPU-only, no torch
MARK_OPEN, MARK_CLOSE = "\x02", "\x03"  # around matched words in snippets
_model, _lock = None, threading.Lock()


def model():
    """Loaded once, from the local cache when possible. None if unavailable (keyword search still works)."""
    global _model
    with _lock:
        if _model is None:
            try:
                os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
                from huggingface_hub import snapshot_download
                from model2vec import StaticModel
                try:
                    path = snapshot_download(MODEL_NAME, local_files_only=True)
                except Exception:
                    path = snapshot_download(MODEL_NAME)
                _model = StaticModel.from_pretrained(path)
            except Exception as e:
                print(f"[paper-index] semantic search disabled: {e}", file=sys.stderr)
                _model = False
    return _model or None


def embed(texts: list[str]) -> np.ndarray | None:
    if not model():
        return None
    v = np.asarray(_model.encode(texts), dtype=np.float32).reshape(len(texts), -1)
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)


def tokens(s: str) -> list[str]:
    return re.findall(r"\w+", s.lower())


def load(p) -> dict:
    """A doc's searchable parts: its text in chunks, and its highlights and notes as (id, shown text, matched text).
    Highlights also match their color's meaning, so searching "question" finds the ones marked as questions."""
    m, text, notes = store.meta(p), store.read(p / "text.txt", ""), store.read(p / "notes.md", "").strip()
    labels = {c["id"]: c["label"] for c in store.load_settings()["categories"]}
    anns = [(0, notes, notes)] if notes else []
    for a in store.read(p / "annotations.json", []):
        shown = "\n".join([a["quote"], a["comment"], *(r["text"] for r in a.get("replies", []))]).strip()
        anns.append((a["id"], shown, f"{labels.get(a['color'], '')} {shown}"))
    words = text.split()
    chunks = [m["title"]] + [" ".join(words[i:i + 200]) for i in range(0, max(len(words) - 40, 1), 160)]
    return {"p": p, "meta": m, "text": text, "anns": anns, "chunks": chunks, "tf": Counter(tokens(text)), "title": set(tokens(m["title"]))}


def vectors(d: dict):
    """(chunk vectors, annotation vectors), cached in vectors.npz until the content changes."""
    sig = hashlib.sha1("\0".join([MODEL_NAME, *d["chunks"], *(t for *_, t in d["anns"])]).encode()).hexdigest()
    path = d["p"] / "vectors.npz"
    try:
        with np.load(path) as z:
            if str(z["sig"]) == sig:
                return z["chunk"], z["ann"]
    except (OSError, KeyError, ValueError):
        pass
    cv = embed(d["chunks"])
    av = embed([t for *_, t in d["anns"]]) if d["anns"] else np.zeros((0, cv.shape[1]), np.float32)
    np.savez(tmp := path.with_name("vectors.tmp.npz"), chunk=cv, ann=av, sig=np.array(sig))
    tmp.replace(path)
    return cv, av


def snippet(text: str, terms: set[str]) -> str:
    pat = re.compile(r"\b(" + "|".join(map(re.escape, sorted(terms, key=len, reverse=True))) + r")\b", re.I)
    m = pat.search(text)
    a, b = (max(0, m.start() - 110), m.end() + 110) if m else (0, 220)
    return pat.sub(lambda x: MARK_OPEN + x.group(0) + MARK_CLOSE, ("…" if a else "") + " ".join(text[a:b].split()) + ("…" if b < len(text) else ""))


def keyword(docs: list[dict], q: str) -> list[tuple]:
    """(score, doc, highlight id or None, snippet), best first: a tf-idf with title and highlight boosts."""
    if not (qt := set(tokens(q))):
        return []
    idf = {t: math.log(1 + len(docs) / (1 + sum(t in d["tf"] or t in d["title"] for d in docs))) for t in qt}
    hits = []
    for d in docs:
        n = max(sum(d["tf"].values()), 1)
        score = sum(idf[t] * (math.log1p(d["tf"][t] * 1000 / n) + 3 * (t in d["title"])) for t in qt)
        ann_score, ann_id, shown = max(((sum(idf[t] for t in qt if t in tokens(match)), i, s) for i, s, match in d["anns"]), default=(0, None, ""))
        if score or ann_score:
            hits.append((score + 2 * ann_score, d, ann_id if ann_score else None, snippet(shown if ann_score else d["text"], qt)))
    return sorted(hits, key=lambda h: -h[0])


def semantic(docs: list[dict], q: str) -> list[tuple]:
    if (qv := embed([q])) is None:
        return []
    hits = []
    for d in docs:
        cv, av = vectors(d)
        cs, as_ = cv @ qv[0], (av @ qv[0] if len(av) else np.array([-1.0]))
        best = d["anns"][as_.argmax()][:2] if as_.max() >= cs.max() else (None, d["chunks"][cs.argmax()])
        hits.append((float(max(cs.max(), as_.max())), d, best[0], best[1][:260] + ("…" if len(best[1]) > 260 else "")))
    return sorted(hits, key=lambda h: -h[0])


def search(q: str, k: int = 15) -> list[dict]:
    docs, fused = [load(p) for p in store.iter_docs()], {}

    def add(d, score, ann_id=None, snip=None):
        f = fused.setdefault(d["p"], {"doc": d["meta"], "score": 0.0, "ann_id": None, "snippet": None})
        f["score"] += score
        if f["snippet"] is None and snip:
            f["snippet"], f["ann_id"] = snip, ann_id

    slug, ql = store.slugify(q), q.strip().lower()
    for d in docs:  # key/value matches first: the doc's name, or its site
        if d["p"].name == slug or (len(slug) >= 3 and d["p"].name.startswith(slug)):
            add(d, 1.0)
        elif len(ql) >= 3 and " " not in ql and ql in (d["meta"].get("site") or ""):
            add(d, 0.8)
    for hits in (keyword(docs, q), semantic(docs, q)):
        for rank, (_, d, ann_id, snip) in enumerate(hits[:k * 3]):
            add(d, 1 / (60 + rank), ann_id, snip)
    return sorted(fused.values(), key=lambda f: -f["score"])[:k]
