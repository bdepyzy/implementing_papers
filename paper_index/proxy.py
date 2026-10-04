"""Every doc is shown in a frame on its own origin, http://<name>.localhost:<port>/ (browsers resolve *.localhost to
loopback), so page scripts are isolated from the app and from each other. /__pi/* serves our own pages (annotator,
reader view, PDF viewer); every other path is the live site, proxied and cached in docs/<name>/archive/.
"""
from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path
from urllib.parse import urlsplit

import requests
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse

from . import store

STATIC = Path(__file__).parent / "static"
PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"
DROP_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive", "set-cookie", "content-type", "location",
                "content-security-policy", "content-security-policy-report-only", "x-frame-options", "strict-transport-security", "etag",
                "cross-origin-opener-policy", "cross-origin-embedder-policy", "cross-origin-resource-policy", "last-modified", "alt-svc", "report-to", "nel"}
MAX_CACHE = 25 * 1024 * 1024
session = requests.Session()


def doc_for_host(host: str) -> str | None:
    name = host.split(":")[0].lower().removesuffix(".localhost")
    return name if store.NAME_RE.fullmatch(name) and (store.DOCS / name / "meta.json").is_file() else None


def cached(name: str, url: str) -> Path:
    return store.DOCS / name / "archive" / hashlib.sha1(url.encode()).hexdigest()


def cache_put(name: str, url: str, ctype: str, body: bytes) -> None:
    (p := cached(name, url)).parent.mkdir(exist_ok=True)
    p.with_suffix(".body").write_bytes(body)
    store.write(p.with_suffix(".json"), {"url": url, "content_type": ctype})


def prime(name: str, url: str, raw_html: str) -> None:
    cache_put(name, url.split("#")[0], "text/html; charset=utf-8", raw_html.encode())


def rewrite(body: bytes, ctype: str, real_origin: str, proxy_origin: str) -> bytes:
    """Point absolute same-site URLs at the proxy, and inject the annotator into HTML."""
    if not any(t in ctype.lower() for t in ("text/html", "text/css", "javascript")):
        return body
    enc = (re.search(r"charset=([\w-]+)", ctype, re.I) or [None, "utf-8"])[1]
    try:
        text = body.decode(enc)
    except (LookupError, UnicodeDecodeError):
        text, enc = body.decode("utf-8", errors="replace"), "utf-8"
    host, proxy_host = urlsplit(real_origin).netloc, urlsplit(proxy_origin).netloc
    text = text.replace(f"https://{host}", proxy_origin).replace(f"http://{host}", proxy_origin).replace(f"//{host}", f"//{proxy_host}")
    if "text/html" in ctype.lower():
        text = re.sub(r"<meta[^>]+http-equiv=[\"']?content-security-policy[^>]*>", "", text, flags=re.I)
        tag, head = f'<script src="/__pi/annotator.js?view=live&origin={real_origin}"></script>', re.search(r"<head[^>]*>", text, re.I)
        text = text[: head.end()] + tag + text[head.end():] if head else tag + text
    return text.encode(enc, errors="replace")


def own_page(name: str, path: str, proxy_origin: str) -> Response:
    d = store.DOCS / name
    if path in ("annotator.js", "file"):
        return FileResponse(STATIC / "annotator.js" if path == "annotator.js" else d / "source.pdf", headers={"cache-control": "no-cache"})
    m = store.meta(d)
    head = (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(m["title"])}</title><style>{(STATIC / "frame.css").read_text()}</style>')
    if path == "pdf":
        return HTMLResponse(f'{head}</head><body class="pdf"><div id="pdf"></div><script src="{PDFJS}"></script><script src="/__pi/annotator.js?view=pdf"></script></body></html>')
    return HTMLResponse(f'{head}<base href="{html.escape(m.get("url") or proxy_origin + "/")}"><script src="{proxy_origin}/__pi/annotator.js?view=reader"></script></head>'
                        f'<body class="reader"><article data-pi-root><h1>{html.escape(m["title"])}</h1>{store.read(d / "content.html", "")}</article></body></html>')


def handle(name: str, method: str, path_qs: str, headers: dict, body: bytes, proxy_origin: str) -> Response:
    if path_qs.startswith("/__pi/"):
        return own_page(name, path_qs[6:].split("?")[0], proxy_origin)
    if not (url := store.meta(store.DOCS / name).get("url")):
        return PlainTextResponse("uploaded document: no live version", 404)
    real = urlsplit(url)
    real_origin = f"{real.scheme}://{real.netloc}"
    upstream, cacheable = real_origin + path_qs, method == "GET" and "range" not in headers
    if cacheable and (hit := cached(name, upstream)).with_suffix(".body").exists():
        ctype = store.read(hit.with_suffix(".json"))["content_type"]
        return Response(rewrite(hit.with_suffix(".body").read_bytes(), ctype, real_origin, proxy_origin), media_type=ctype or None)
    fwd = {k: headers[k] for k in ("user-agent", "accept", "accept-language", "content-type", "range", "x-requested-with") if k in headers}
    fwd |= {"referer": upstream} | ({"origin": real_origin} if method not in ("GET", "HEAD") else {})
    try:
        r = session.request(method, upstream, headers=fwd, data=body or None, stream=True, timeout=30)
    except requests.RequestException as e:
        return PlainTextResponse(f"paper-index: could not reach {upstream}: {e}", 502)
    if r.history and (final := urlsplit(r.url)).netloc == real.netloc and r.url != upstream:  # same-site redirect: move the frame too
        r.close()
        return RedirectResponse(proxy_origin + final.path + (f"?{final.query}" if final.query else ""), 302)
    ctype, out = r.headers.get("content-type", ""), {k: v for k, v in r.headers.items() if k.lower() not in DROP_HEADERS}
    if "range" in headers or int(r.headers.get("content-length") or 0) > MAX_CACHE or ctype.startswith(("video/", "audio/")):
        return StreamingResponse(r.iter_content(65536), status_code=r.status_code, headers=out, media_type=ctype or None)
    if cacheable and r.status_code == 200 and len(r.content) <= MAX_CACHE:
        cache_put(name, upstream, ctype, r.content)
    return Response(rewrite(r.content, ctype, real_origin, proxy_origin), status_code=r.status_code, headers=out, media_type=ctype or None)
