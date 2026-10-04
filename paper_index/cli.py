"""paper-index: save web pages and papers, highlight them, find them again. With no command it opens the app."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

from . import store

APP_NAME = "Paper Index"  # also the page <title>, which is how its browser window is found
HOST = "127.0.0.1"  # loopback only: nothing is reachable from the network
URL = f"http://{HOST}:{store.PORT}/"
XDG_DATA = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()

def serve() -> None:
    import uvicorn
    uvicorn.run("paper_index.server:app", host=HOST, port=store.PORT, log_level="warning")


def focused_browser() -> str | None:
    """The browser in the focused window, if the window manager can tell us (Hyprland, or X11 via xdotool)."""
    try:
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            name = json.loads(subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True).stdout or "{}").get("class", "")
        else:
            name = subprocess.run(["xdotool", "getactivewindow", "getwindowclassname"], capture_output=True, text=True).stdout.strip()
    except (OSError, ValueError):
        return None
    name = name.lower().removesuffix(".desktop")
    known = ("chrome", "chromium", "firefox", "brave", "edge", "vivaldi", "opera", "librewolf", "zen")
    return shutil.which(name) if any(b in name for b in known) else None


def go_to_open_window() -> bool:
    """Jump to a browser window that is showing the app (its title starts with APP_NAME), on any workspace."""
    try:
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            clients = json.loads(subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True).stdout or "[]")
            window = next((c["address"] for c in clients if c.get("title", "").startswith(APP_NAME)), None)
            return bool(window) and subprocess.run(["hyprctl", "dispatch", "focuswindow", f"address:{window}"], capture_output=True).returncode == 0
        return subprocess.run(["xdotool", "search", "--name", f"^{APP_NAME}", "windowactivate"], capture_output=True).returncode == 0
    except OSError:
        return False


def open_app() -> None:
    """Go to the window showing the app; if there's none, open it in the browser you're looking at (else the
    default one), where you left off. If the background service isn't running, serve until Ctrl-C."""
    import webbrowser
    url = URL
    try:
        urllib.request.urlopen(url, timeout=1)
        if go_to_open_window():
            return
        url += store.read(store.HOME / "session.json", {}).get("hash", "")
        if browser := focused_browser():
            subprocess.Popen([browser, url], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            webbrowser.open(url)
    except OSError:
        print(f"{APP_NAME}: {url}  (Ctrl-C to stop; `paper-index install` keeps it running in the background)")
        threading.Timer(1, webbrowser.open, [url]).start()
        serve()


def install() -> None:
    """Add the app to the launcher, and keep its server running in the background (systemd user service)."""
    exe = shutil.which("paper-index") or str(Path(sys.executable).with_name("paper-index"))
    entry = XDG_DATA / "applications" / "paper-index.desktop"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(f"[Desktop Entry]\nType=Application\nName={APP_NAME}\nComment=Read, highlight, remember\nExec={exe}\n"
                     f"Icon={Path(__file__).parent / 'static' / 'icon.svg'}\nTerminal=false\nCategories=Office;Education;\n")
    unit = XDG_CONFIG / "systemd" / "user" / "paper-index.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(f"[Unit]\nDescription={APP_NAME}\n\n[Service]\nExecStart={exe} serve\n"
                    f"Environment=PAPER_INDEX_HOME={store.HOME}\nEnvironment=PAPER_INDEX_PORT={store.PORT}\n"
                    "Restart=on-failure\n\n[Install]\nWantedBy=default.target\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "paper-index"], check=False)
    print(f"Added to your app launcher ({entry}) and started the background service ({unit}).")


def show(d: dict) -> str:
    lines = [d["title"], d.get("url") or "", d["notes"]]
    for a in d["annotations"]:
        lines += [f"\n  [{a['color']}] “{' '.join(a['quote'].split())}”", *([f"    {a['comment']}"] if a["comment"] else []),
                  *(f"    ↳ {r['text']}" for r in a.get("replies", []))]
    return "\n".join(line for line in lines if line)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="paper-index", description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("open", help="open the app in your browser (the default)")
    sub.add_parser("serve", help="run just the server (what the background service runs)")
    sub.add_parser("install", help="run it in the background and add it to your app launcher")
    p = sub.add_parser("add", help="save a URL, or a local PDF or .html file")
    p.add_argument("source")
    p.add_argument("--name")
    sub.add_parser("get", help="show a doc's notes and highlights (by name, partial name, URL or title)").add_argument("key")
    sub.add_parser("ls", help="list saved docs")
    sub.add_parser("search", help="search docs, highlights and notes").add_argument("query", nargs="+")
    sub.add_parser("rm", help="delete a doc").add_argument("key")
    a = ap.parse_args(argv)
    try:
        if a.cmd in (None, "open"):
            open_app()
        elif a.cmd == "serve":
            serve()
        elif a.cmd == "install":
            install()
        elif a.cmd == "add":
            from . import ingest
            path = Path(a.source).expanduser()
            doc = ingest.add_file(path.read_bytes(), path.name, a.name) | {"created": True} if path.is_file() else ingest.add_url(a.source, a.name)
            print(("saved as " if doc["created"] else "already saved as ") + doc["name"])
        elif a.cmd == "get":
            print(show(store.get_doc(a.key)))
        elif a.cmd == "ls":
            print("\n".join(f"{d['name']:<40} {d.get('site') or 'local':<22} {d['title'][:70]}" for d in store.list_docs()))
        elif a.cmd == "search":
            from . import search
            for h in search.search(" ".join(a.query), 10):
                snippet = (h["snippet"] or "").replace(search.MARK_OPEN, "\033[1m").replace(search.MARK_CLOSE, "\033[0m")
                print(f"{h['doc']['name']:<40} {h['doc']['title'][:70]}\n    {' '.join(snippet.split())[:240]}")
        elif a.cmd == "rm":
            store.delete(a.key)
    except (store.NotFound, ValueError) as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        pass
