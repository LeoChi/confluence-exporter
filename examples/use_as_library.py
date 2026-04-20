"""Use confluence-exporter as a library, not as a CLI.

This script shows the **public API** you can import into your own Python
code. It is equivalent to running::

    confluence-exporter export
    confluence-exporter convert
    confluence-exporter merge

…but with full control over the pipeline — you can skip steps, add
your own progress UI, post-process the output, embed it inside a larger
workflow, etc.

Install:

    pip install confluence-exporter
    # or with a PDF engine that produces nicer output:
    pip install "confluence-exporter[playwright]"
    playwright install chromium

Run:

    python examples/use_as_library.py
"""

from __future__ import annotations

from pathlib import Path

# ---- 1. Configure -----------------------------------------------------------
#
# You can either:
#   a) load a config.json you already created via the CLI, or
#   b) build an AppConfig in code.
#
# Below is option (b) — no config file needed.

from confluence_exporter import (
    AppConfig,
    ConfluenceClient,
    OutputConverter,
    PDFMerger,
    SpaceExporter,
    load_config,  # noqa: F401  — available if you prefer option (a)
)

cfg = AppConfig()

# --- Confluence target ---
cfg.confluence.base_url = "https://your-tenant.atlassian.net"
cfg.confluence.space_key = "ABC"

# --- Authentication (pick ONE mode) ---
# (1) API token (Atlassian Cloud, if your tenant allows API tokens):
cfg.confluence.auth_mode = "api_token"
cfg.confluence.email = "you@example.com"
cfg.confluence.api_token = "YOUR-API-TOKEN"

# (2) Browser cookie (works with SSO / enterprise tenants that block API tokens):
# cfg.confluence.auth_mode = "browser_cookie"
# cfg.confluence.cookies = {
#     # Paste the session cookie value(s) from your logged-in browser.
#     # The exact name varies per tenant — common ones:
#     "cloud.session.token": "…",
#     "tenant.session.token": "…",
# }

# (3) Personal Access Token (Server / Data Center):
# cfg.confluence.auth_mode = "pat"
# cfg.confluence.personal_access_token = "YOUR-PAT"

# --- Export options ---
cfg.export.format = "html"          # html is fastest and most flexible for re-conversion
cfg.export.output_path = "./output"
cfg.export.include_attachments = True
cfg.export.skip_unchanged = True    # incremental: only refresh pages whose version changed

errs = cfg.validate()
if errs:
    raise SystemExit("Config errors:\n  - " + "\n  - ".join(errs))


# ---- 2. Optional: test the connection before doing real work ----------------

client = ConfluenceClient.from_config(cfg.confluence)
user = client.test_connection()
print(f"Authenticated as: {user.get('displayName') or user.get('username')}")
space = client.get_space(cfg.confluence.space_key)
print(f"Space OK: {space.get('name')} ({space.get('key')})")


# ---- 3. Export: download pages + attachments to disk ------------------------


def on_export_progress(title: str, i: int, total: int) -> None:
    print(f"  [{i:>4}/{total}]  {title}")


exporter = SpaceExporter(cfg, client, progress=on_export_progress)
written, skipped, failed = exporter.run()
print(f"Export done: {written} written, {skipped} skipped, {failed} failed")


# ---- 4. Convert: clean HTML → PDF/DOCX with attachments merged --------------

converter = OutputConverter(
    output_root=Path(cfg.export.output_path),
    target_format="pdf",
    engine="auto",                   # 'auto' picks playwright > weasyprint > xhtml2pdf
    merge_pdf_attachments=True,      # PDF attachments get appended as an appendix
    progress=lambda n, i, t: print(f"  converting [{i}/{t}]  {n}"),
)
ok_n, fail_n = converter.run()
print(f"Convert done: {ok_n} ok, {fail_n} failed -> {converter.converted_root}")


# ---- 5. Merge: consolidate per-page PDFs into volumes with TOC + bookmarks --

merger = PDFMerger(
    source_root=converter.converted_root,
    dest_root=Path("./output_volumes"),
    mode="per_section",              # one PDF per top-level section (NotebookLM-friendly)
    engine="auto",
)
vol_ok, vol_fail = merger.run()
print(f"Merge done: {vol_ok} volumes, {vol_fail} failed -> ./output_volumes")
