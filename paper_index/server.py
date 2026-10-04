"""Local web app, running only while the app window is open (see cli.app).

The API is one route: POST /api/<action> with JSON arguments calls ACTIONS[action](**arguments).
"""
from __future__ import annotations

import threading
from pathlib import Path
from urllib.parse import urlsplit

import requests
from fastapi import Body, FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from . import ingest, proxy, search, store

ACTIONS = {
    "settings": store.load_settings, "save_settings": lambda **s: store.save_settings(s),
    "list": store.list_docs, "search": search.search, "get": store.get_doc, "rename": store.rename, "delete": store.delete,
    "open": ingest.add_url,
    "notes": store.save_notes, "here": store.here,
    "highlight": store.highlight, "edit_highlight": store.edit_highlight, "remove_highlight": store.remove_highlight,
}
LOOPBACK = ("127.0.0.1", "localhost", "::1")
app = FastAPI(title="paper-index")
for error, status in ((store.NotFound, 404), (ValueError, 400), (TypeError, 400), (requests.RequestException, 502)):
    app.add_exception_handler(error, lambda _, e, status=status: PlainTextResponse(str(e), status))


@app.on_event("startup")
def startup() -> None:
    threading.Thread(target=search.model, daemon=True).start()  # load the embedding model in the background


@app.middleware("http")
async def route(request: Request, call_next):
    host = request.headers.get("host", "")
    hostname = urlsplit("//" + host).hostname or ""
    if hostname.endswith(".localhost"):  # a doc's own origin: see proxy.py
        if not (name := await run_in_threadpool(proxy.doc_for_host, host)):
            return PlainTextResponse("unknown document", 404)
        path_qs = request.scope.get("raw_path", request.url.path.encode()).decode("latin-1") + (f"?{request.url.query}" if request.url.query else "")
        return await run_in_threadpool(proxy.handle, name, request.method, path_qs, {k.lower(): v for k, v in request.headers.items()},
                                       await request.body(), f"http://{host}")
    if hostname not in LOOPBACK:  # e.g. DNS rebinding: another site pointing its name at us
        return PlainTextResponse("unknown host", 403)
    origin = request.headers.get("origin")  # pages shown in a doc frame must not be able to change your library
    if request.method != "GET" and origin and urlsplit(origin).netloc != host:
        return PlainTextResponse("cross-origin request refused", 403)
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "static" / "index.html").read_text("utf-8")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    return await run_in_threadpool(ingest.add_file, data, file.filename or "upload")


@app.post("/api/{action}")
def call(action: str, args: dict = Body(default={})):
    if action not in ACTIONS:
        raise store.NotFound(f"action {action}")
    return ACTIONS[action](**args)
