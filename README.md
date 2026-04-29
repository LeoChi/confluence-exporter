<!-- markdownlint-disable MD033 MD041 -->
<div align="center">

# Confluence Exporter

**Export an entire Confluence space to PDF, DOCX, Markdown or HTML — with attachments embedded, optional consolidated volumes, and an interactive terminal UI.**

[![PyPI](https://img.shields.io/pypi/v/confluence-space-exporter.svg)](https://pypi.org/project/confluence-space-exporter/)
[![Python](https://img.shields.io/pypi/pyversions/confluence-space-exporter.svg)](https://pypi.org/project/confluence-space-exporter/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

Built for teams that need to **archive, migrate, or feed their wiki into an LLM** (NotebookLM, RAG pipelines, LLM fine-tuning). The tool produces clean, self-contained PDFs with embedded images and merged attachment appendices — no missing links, no 0 KB files, no orphaned binary blobs.

---

## Highlights

- 🔐 **Three auth strategies**: API token (Basic), Personal Access Token (Bearer), or **browser-cookie paste** — works on SSO-locked tenants where API tokens are disabled. Cookie parser is generic: paste the full `Cookie:` header from DevTools and any session-cookie name (`cloud.session.token`, `tenant.session.token`, …) works.
- 🖨️ **Pluggable PDF engines** with automatic fallback: **Playwright → WeasyPrint → xhtml2pdf**. Pick one explicitly or let the tool auto-detect the best one you have installed.
- 📎 **Attachments really embedded**: PDFs get merged as appendix pages into each document; images are inlined; others are listed with links.
- 📚 **Consolidated volumes**: merge the per-page PDFs of a space into a few big PDFs with a generated **Table of Contents** and a **hierarchical PDF bookmark outline** — upload-ready for NotebookLM.
- 🪟 **Windows long-path safe**: deep Confluence hierarchies that exceed `MAX_PATH` (260 chars) are handled via `\\?\` prefixing and a `_flat` fallback bucket.
- ⚡ **Resumable**: a per-space lockfile skips pages that haven't changed, so re-runs take seconds.
- 🎨 **Nice terminal UI**: colored prompts, progress bars, summary tables (powered by [Rich](https://github.com/Textualize/rich) + [Typer](https://typer.tiangolo.com/)).

---

## Installation

```bash
pip install confluence-space-exporter
```

For best-quality PDFs (recommended), also install Playwright:

```bash
pip install "confluence-space-exporter[playwright]"
playwright install chromium
```

All engines in one shot:

```bash
pip install "confluence-space-exporter[all]"
playwright install chromium
```

Requires Python **3.10+**.

---

## Three ways to use it

The same codebase ships with three entry surfaces — the business logic lives in the library; CLI and GUI are thin adapters on top.

| Mode               | When to use it                                                  | Launch                                             |
| ------------------ | --------------------------------------------------------------- | -------------------------------------------------- |
| 🖱️ **Desktop app** | Prefer a window with forms, file pickers and a live log pane    | `confluence-exporter-gui` *(alias: `cfx-gui`)*     |
| 💻 **CLI / TUI**   | SSH sessions, scripts, CI pipelines, or a rich terminal UI      | `confluence-exporter` *(alias: `cfx`)*             |
| 📦 **Library**     | Embed the export inside your own Python code / data pipeline    | `from confluence_exporter import SpaceExporter, …` |

### Desktop app

```bash
confluence-exporter-gui
```

A Tkinter window with tabs for **Connection → Export → Convert → Merge → Diagnose**, a progress bar, and a live log pane. All long tasks run on a background thread so the UI stays responsive. No extra dependencies — Tkinter ships with Python. (On some Linux distros you may need `sudo apt install python3-tk`.)

### CLI

```bash
# Interactive menu (recommended first run)
confluence-exporter

# Short alias
cfx
```

The tool walks you through auth setup, target space, and format, and saves your choices to `config.json`. Subsequent runs re-use it.

Non-interactive / scripted:

```bash
cfx export   --space MYKEY --format pdf --output ./out -y
cfx convert  ./out --engine playwright --merge -y
cfx merge    ./out_converted ./out_volumes --mode per_section -y

cfx diagnose        # check installed engines + credentials
cfx init-config     # edit / (re)create config.json
```

Run `cfx <command> --help` for all options.

### Library

```python
from pathlib import Path
from confluence_exporter import (
    AppConfig, ConfluenceClient, SpaceExporter, OutputConverter, PDFMerger,
)

cfg = AppConfig()
cfg.confluence.base_url  = "https://your-tenant.atlassian.net"
cfg.confluence.space_key = "ABC"
cfg.confluence.auth_mode = "api_token"
cfg.confluence.email     = "you@example.com"
cfg.confluence.api_token = "…"

client = ConfluenceClient.from_config(cfg.confluence)
SpaceExporter(cfg, client).run()

OutputConverter(
    output_root=Path(cfg.export.output_path),
    target_format="pdf",
    engine="auto",
).run()

PDFMerger(
    source_root=Path(cfg.export.output_path + "_converted"),
    dest_root=Path("./volumes"),
    mode="per_section",
).run()
```

A full worked example — including progress callbacks and all three auth modes — is in [`examples/use_as_library.py`](examples/use_as_library.py).

---

## Three modes, end to end

### 1. **Export** — download a Confluence space

```bash
cfx export
```

Writes pages + attachments to the output folder:

```
output/
└── MySpace/
    ├── Overview/
    │   ├── Introduction.html
    │   └── Architecture.html
    ├── attachments/
    │   ├── Introduction/
    │   │   └── diagram.png
    │   └── _flat/
    │       └── 12345_long-attachment-name.pdf
    └── _flat/                      # pages whose path was too long for Windows
        └── Nested_Deep_Page_9999.html
```

#### Incremental updates

Re-running `export` is **safe and fast**: it compares the live Confluence space against the per-space lockfile and only downloads what actually changed. Each page falls into one of four buckets:

| State | Meaning | Action |
| --- | --- | --- |
| **NEW** | Page exists in Confluence, not in the lockfile | Download |
| **UPDATED** | Newer version on Confluence (or local file is missing) | Re-download |
| **UNCHANGED** | Same version, file still on disk | Skip |
| **DELETED-UPSTREAM** | In the lockfile but no longer in Confluence | Optionally remove (`cleanup_stale: true`) |

To **preview** what an export would do — without downloading anything — use the `status` command:

```bash
cfx status                     # summary only
cfx status --titles            # also list the actual page titles
cfx status --titles -n 50      # bump the per-bucket cap from 20 to 50
```

Sample output:

```
Diff
┏━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━┓
┃ Status                ┃ #    ┃
┡━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━┩
│ New                   │   3  │
│ Updated               │  12  │
│ Unchanged             │ 247  │
│ Deleted upstream      │   1  │
│ Total in Confluence   │ 262  │
└───────────────────────┴──────┘
→ Running export would download 3 new + 12 updated page(s).
```

The GUI exposes the same thing as a "🔍 Check status" button on the Export tab.

> **Tip**: if you delete a PDF on disk, the next run notices and re-downloads it (the lockfile alone isn't trusted — we also check the file is actually there).

### 2. **Convert** — HTML → PDF / DOCX with embedded attachments

```bash
cfx convert ./output
```

Produces `./output_converted/` mirroring the source tree. Each PDF has its page's PDF attachments merged as appendix pages. The conversion tolerates deep Windows paths (renders via `%TEMP%` and moves into place) and validates every output via magic-byte + size checks — **no 0 KB files slip through**.

### 3. **Merge** — consolidated volumes for NotebookLM / archival

```bash
cfx merge ./output_converted ./output_volumes
```

Three grouping modes:

| Mode           | Output                                           | Best for                                                |
| -------------- | ------------------------------------------------ | ------------------------------------------------------- |
| `per_section`  | One PDF per top-level folder of each space       | NotebookLM sources (smaller, focused)                   |
| `per_space`    | One PDF per Confluence space                     | Sharing a whole space as a single file                  |
| `single`       | One PDF for everything                           | Archival / grep-friendly single file                    |

Each volume contains a generated **Table of Contents** page (page numbers + nesting) and a **PDF outline** that every reader (Acrobat, Edge, Chrome, Foxit…) shows as a navigation sidebar.

---

## Authentication

### Option 1 — Atlassian API token (easiest, if your admin allows it)

```json
"auth_mode": "api_token",
"email": "you@company.com",
"api_token": "ATATT3x…"
```

Get a token at <https://id.atlassian.com/manage-profile/security/api-tokens>.

### Option 2 — Browser cookie (for SSO-only tenants)

```json
"auth_mode": "browser_cookie",
"cookies": {
  "cloud.session.token": "eyJ…",
  "atlassian.xsrf.token": "…"
}
```

Easiest way to set this up:

```bash
cfx init-config   # or choose menu option 5
```

The tool walks you through copying the full `Cookie:` header from DevTools — it then parses and forwards every cookie the browser would send, so it doesn't matter whether your tenant uses `cloud.session.token`, `tenant.session.token`, `JSESSIONID` or something else.

### Option 3 — Personal Access Token (Server / Data Center)

```json
"auth_mode": "pat",
"personal_access_token": "NjAxM…"
```

Sent as `Authorization: Bearer …`.

---

## Configuration

A full `config.json` looks like [`examples/config.example.json`](examples/config.example.json). All fields have sane defaults and are overridable via CLI flags.

Keys starting with `_` are treated as inline documentation and ignored at load time.

---

## Standalone executable (no Python required for end users)

If you want to ship a double-clickable `.exe` / `.app` for users who don't have Python installed, bundle it with PyInstaller:

```bash
pip install "confluence-space-exporter[all]" pyinstaller
# Windows / macOS / Linux (run on the target OS):
pyinstaller --name ConfluenceExporter --windowed --onefile ^
  --collect-all confluence_exporter ^
  -m confluence_exporter.gui
```

The resulting `dist/ConfluenceExporter.exe` (or `.app` on macOS) embeds Python and all dependencies. For the Playwright engine specifically, Chromium binaries are large and best installed separately after first launch — in a bundled build, prefer `weasyprint` or `xhtml2pdf` out of the box.

---

## Troubleshooting

| Symptom                                          | Likely cause / fix                                                                                                           |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| `HTTP 401 Unauthorized`                          | API token disabled by admin → switch to `browser_cookie` mode.                                                                |
| `HTTP 403 Forbidden` when exporting PDFs         | Confluence's native PDF endpoint is disabled; the tool will fall through to local rendering automatically.                    |
| `Playwright Chromium binary not installed`       | `python -m playwright install chromium`.                                                                                      |
| `[Errno 2] No such file or directory` (Windows)  | Path exceeds MAX_PATH. The tool falls back to `_flat/` automatically — enable it in your run.                                 |
| 0 KB PDFs                                        | The older engine couldn't render a page. Install Playwright and rerun: `cfx convert --engine playwright`.                     |

Run `cfx diagnose` any time to see what's installed and confirm your credentials.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). PRs welcome!

---

## License

MIT — see [LICENSE](LICENSE).
