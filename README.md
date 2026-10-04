# paper-index

Save web pages and PDFs, highlight them, take notes, and search them later.

## Setup

```bash
git clone https://github.com/bdepyzy/implementing_papers.git paper-index
cd paper-index
uv tool install -e .
paper-index install
```

`paper-index install` keeps it running in the background (also after a reboot) and adds it to your app launcher.

## Open

Go to http://127.0.0.1:8765, or run `paper-index`.

Bind `paper-index` to a key (e.g. Super+I) in your desktop's keyboard settings. It jumps to the window showing
Paper Index, or opens it in the browser you're on, where you left off. Keep Paper Index in its own window so the
shortcut can always find it.

## Use

- Paste a link anywhere to save it. Type in the top box to search (`/` jumps there).
- Select text to highlight it. Write a note or pick a color to keep it.
- Click a highlight to reply, resolve or remove it.
- ⚙ sets what each color means.

## Your library

By default your library is in `~/.paper_index`, so anyone who clones this repo starts with an empty one.

To keep your library in this repo instead, and have it on every laptop you set up this way, use this in place of the
last setup step:

```bash
PAPER_INDEX_HOME=$PWD/library paper-index install
```

A minute after your last change, each changed article is committed as `saved "<title>"` and pushed. Saves from your
other laptops are pulled when it starts and every 10 minutes.

## Settings

| Variable | Default |
|---|---|
| `PAPER_INDEX_HOME` | `~/.paper_index` (your library, as plain files) |
| `PAPER_INDEX_PORT` | `8765` |

Set them before running `paper-index install`.

## Uninstall

```bash
systemctl --user disable --now paper-index
uv tool uninstall paper-index
```
