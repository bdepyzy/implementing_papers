"""Turn a URL or uploaded file into a stored document."""
from __future__ import annotations

import io
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from . import proxy, store

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
ARXIV = re.compile(r"https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/([^?#]+?)(?:\.pdf)?/?(?:[?#].*)?$")


def get(url: str) -> requests.Response:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    if (r.encoding or "").lower() in ("", "iso-8859-1"):
        r.encoding = r.apparent_encoding
    return r


def parse_html(raw: str, base: str) -> dict:
    """Title, reader-view HTML (readability, sanitized) and plain text for search."""
    from readability import Document
    page = BeautifulSoup(raw, "html.parser")
    try:
        soup = BeautifulSoup(Document(raw).summary(html_partial=True), "html.parser")
    except Exception:  # readability gives up on some pages: keep the whole body
        soup = BeautifulSoup(str(page.body or page), "html.parser")
    for t in soup(["script", "style", "iframe", "object", "embed", "form", "input", "button", "link", "meta"]):
        t.decompose()
    for t in soup.find_all(True):
        if t.name == "img" and not t.get("src", "").startswith("http"):
            t["src"] = t.get("data-src") or t.get("data-lazy-src") or t.get("src", "")
        t.attrs = {k: v for k, v in t.attrs.items() if k in ("href", "src", "alt", "title", "colspan", "rowspan", "id")}
        for a in ("href", "src"):
            if t.get(a):
                t[a] = urljoin(base, t[a])
                if not t[a].startswith(("http://", "https://")):
                    del t[a]
    og = page.find("meta", property="og:title")
    title = (og and og.get("content")) or (page.title and page.title.get_text(strip=True)) or base
    return {"title": title.strip(), "content": str(soup), "text": soup.get_text(" ", strip=True)}


def parse_pdf(data: bytes, fallback: str) -> dict:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join(p.extract_text() or "" for p in reader.pages)
    title = ((reader.metadata or {}).get("/Title") or "").strip()
    if len(title) < 4 or re.search(r"\.(pdf|dvi|tex|docx?)$|^untitled|^microsoft word", title, re.I):
        title = next((ln.strip() for ln in text.splitlines() if 10 <= len(ln.strip()) <= 200), fallback)
    return {"title": title, "content": data, "text": text}


def fetch(url: str) -> tuple[str, dict, str | None]:
    """(kind, parsed, raw_html). arXiv abs links fetch the PDF but keep the abstract page's title."""
    title, target = None, url
    if m := ARXIV.match(url):
        target = f"https://arxiv.org/pdf/{m.group(1)}"
        try:
            title = BeautifulSoup(get(f"https://arxiv.org/abs/{m.group(1)}").text, "html.parser").find("meta", attrs={"name": "citation_title"})["content"]
        except (requests.RequestException, TypeError):
            pass
    r = get(target)
    if "pdf" in r.headers.get("content-type", "").lower() or r.content[:5] == b"%PDF-":
        return "pdf", parse_pdf(r.content, url) | ({"title": title.strip()} if title else {}), None
    return "html", parse_html(r.text, url), r.text


def add_url(url: str) -> dict:
    """The doc for this URL, with created=False if it was already saved."""
    url = url.strip() if re.match(r"https?://", url.strip()) else "https://" + url.strip()
    if p := store.find_by_url(url):
        return store.meta(p) | {"created": False}
    kind, parsed, raw = fetch(url)
    doc = store.add_doc(kind=kind, url=url, **parsed)
    if raw:
        proxy.prime(doc["name"], url, raw)  # the first Live view is instant and works offline
    return doc | {"created": True}


def add_file(data: bytes, filename: str) -> dict:
    """An uploaded PDF or saved web page (.html)."""
    if filename.lower().endswith(".pdf") or data[:5] == b"%PDF-":
        return store.add_doc(kind="pdf", **parse_pdf(data, filename))
    return store.add_doc(kind="html", **parse_html(data.decode("utf-8", errors="replace"), "about:blank"))
