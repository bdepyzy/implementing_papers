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

## Command line

```bash
paper-index add <url-or-file>
paper-index get <name>
paper-index ls
paper-index search <query>
paper-index rm <name>
```

## Settings

| Variable | Default |
|---|---|
| `PAPER_INDEX_HOME` | `~/.paper_index` (your data, as plain files) |
| `PAPER_INDEX_PORT` | `8765` |

Set them before running `paper-index install`.

## Uninstall

```bash
systemctl --user disable --now paper-index
uv tool uninstall paper-index
```
