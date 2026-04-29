"""Typer-based CLI entry point.

Usage:

.. code-block:: text

    confluence-exporter                       # interactive menu
    confluence-exporter export                # run export (prompts for missing)
    confluence-exporter convert [PATH]        # convert an output folder to PDF/DOCX
    confluence-exporter merge   [SRC] [DST]   # build consolidated volumes
    confluence-exporter init-config           # scaffold a config.json
    confluence-exporter diagnose              # check installed engines + connection
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.traceback import install as _install_rich_tb

from confluence_exporter import __version__
from confluence_exporter.auth import (
    KNOWN_ATLASSIAN_SESSION_COOKIES,
    parse_cookie_header,
)
from confluence_exporter.client import ConfluenceClient, ConfluenceError
from confluence_exporter.config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    load_config,
    save_config,
)
from confluence_exporter.converter import OutputConverter
from confluence_exporter.exporter import SpaceExporter
from confluence_exporter.logging_utils import setup_logging
from confluence_exporter.merger import PDFMerger
from confluence_exporter.pdf_engines import detect_engine, engine_names
from confluence_exporter.ui import (
    ask,
    ask_multiline,
    ask_yes_no,
    banner,
    console,
    error,
    info,
    ok,
    print_spaces_table,
    progress_bar,
    section,
    summary_table,
    warn,
)

_install_rich_tb(show_locals=False, max_frames=5)


app = typer.Typer(
    help="Export a Confluence space to PDF/DOCX/MD/HTML, with conversion and merging.",
    add_completion=False,
    rich_markup_mode="rich",
    invoke_without_command=True,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"confluence-exporter {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-V",
        callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_PATH),
        "--config", "-c",
        help="Path to config file.",
    ),
    log_level: str = typer.Option(
        "INFO", "--log-level", "-l",
        help="DEBUG | INFO | WARNING | ERROR",
    ),
) -> None:
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    ctx.obj["log_level"] = log_level
    setup_logging(log_level)
    if ctx.invoked_subcommand is None:
        # No subcommand: show interactive menu
        interactive_menu(ctx)


def _load(ctx: typer.Context) -> AppConfig:
    return load_config(ctx.obj["config_path"])


def _save(ctx: typer.Context, cfg: AppConfig) -> None:
    save_config(cfg, ctx.obj["config_path"])


# ---------------------------------------------------------------------------
# Interactive authentication setup
# ---------------------------------------------------------------------------


def _interactive_auth(cfg: AppConfig) -> None:
    section("Authentication")

    info("Choose how to authenticate to Confluence:")
    console.print("   [accent]1[/accent]) [primary]api_token[/primary]      "
                  "— email + Atlassian API token (tenants that allow it)")
    console.print("   [accent]2[/accent]) [primary]browser_cookie[/primary] "
                  "— paste cookies from your logged-in browser (works with SSO)")
    console.print("   [accent]3[/accent]) [primary]pat[/primary]            "
                  "— Personal Access Token (Server / Data Center)")

    default_idx = {"api_token": "1", "browser_cookie": "2", "pat": "3"}.get(
        cfg.confluence.auth_mode, "1"
    )
    choice = ask("Mode", default=default_idx, choices=["1", "2", "3"])
    cfg.confluence.auth_mode = {"1": "api_token", "2": "browser_cookie", "3": "pat"}[choice]

    if cfg.confluence.auth_mode == "api_token":
        cfg.confluence.email = ask("Email", default=cfg.confluence.email)
        cfg.confluence.api_token = ask(
            "API token", default=cfg.confluence.api_token, password=True
        )

    elif cfg.confluence.auth_mode == "pat":
        cfg.confluence.personal_access_token = ask(
            "Personal Access Token",
            default=cfg.confluence.personal_access_token,
            password=True,
        )

    else:  # browser_cookie
        _guide_browser_cookie_setup(cfg)


def _guide_browser_cookie_setup(cfg: AppConfig) -> None:
    info("Steps to copy your session cookies from the browser:")
    console.print(
        "  1. Log in to your Confluence in a browser.\n"
        "  2. Open DevTools ([accent]F12[/accent]) → [primary]Network[/primary] tab.\n"
        "  3. Refresh the page. Click any request to atlassian.net.\n"
        "  4. In [primary]Request Headers[/primary], copy the whole [accent]Cookie:[/accent] "
        "header value.\n"
        "  5. Paste it below."
    )
    console.print(
        "[muted]Accepted formats:[/muted]\n"
        "  • Full 'Cookie:' header (with or without the prefix)\n"
        "  • Semicolon-separated 'name=value; name=value'\n"
        "  • One 'name=value' per line (finish with END)\n"
        "  • A JSON object {\"name\": \"value\", ...}"
    )
    raw = ask_multiline(
        "Paste your cookie header (or line-by-line pairs):",
        end_marker="END",
    )
    cookies = parse_cookie_header(raw)
    if not cookies:
        error("No cookies parsed. Try again with 'init-config'.")
        raise typer.Exit(1)
    hits = [c for c in cookies if c in KNOWN_ATLASSIAN_SESSION_COOKIES
            or "session" in c.lower() or "token" in c.lower()]
    cfg.confluence.cookies = cookies
    ok(f"Parsed {len(cookies)} cookie(s).")
    if hits:
        info(f"Likely session cookies: {', '.join(hits)}")
    else:
        warn(
            "None of the cookie names look like an Atlassian session token "
            "(e.g. cloud.session.token, tenant.session.token). They may still "
            "work — we forward every cookie you pasted."
        )


def _interactive_confluence_target(cfg: AppConfig) -> None:
    section("Confluence target")
    cfg.confluence.base_url = ask(
        "Confluence base URL (e.g. https://your-tenant.atlassian.net)",
        default=cfg.confluence.base_url or "https://",
    ).rstrip("/")
    cfg.confluence.space_key = ask(
        "Space key (the short uppercase code, not the display name)",
        default=cfg.confluence.space_key,
    ).strip()


def _confirm_credentials(cfg: AppConfig) -> bool:
    section("Verifying credentials")
    try:
        client = ConfluenceClient.from_config(
            cfg.confluence,
            request_delay_seconds=cfg.export.request_delay_seconds,
        )
        user = client.test_connection()
        ok(f"Authenticated as {user.get('displayName') or user.get('username') or '?'}")
    except ConfluenceError as e:
        error(str(e))
        return False
    except Exception as e:
        error(f"Unexpected error: {e}")
        return False

    # Optionally verify the space
    try:
        sp = client.get_space(cfg.confluence.space_key)
        ok(f"Space '{sp.get('name')}' ({sp.get('key')})")
        return True
    except ConfluenceError as e:
        error(f"Space check failed: {e}")
        if ask_yes_no("List all spaces you have access to?", default=True):
            try:
                spaces = client.list_spaces()
                print_spaces_table(spaces)
                new_key = ask("Pick a space KEY from the table above",
                              default=cfg.confluence.space_key)
                cfg.confluence.space_key = new_key.strip()
                return _confirm_credentials(cfg)
            except Exception as ee:
                error(str(ee))
        return False


# ---------------------------------------------------------------------------
# Interactive menus — delegates to the subcommands below
# ---------------------------------------------------------------------------


def interactive_menu(ctx: typer.Context) -> None:
    banner(subtitle=f"v{__version__}  •  type ? anytime for help")
    while True:
        console.print()
        console.print("[primary]Choose an action:[/primary]")
        console.print("  [accent]1[/accent]  Export a Confluence space")
        console.print("  [accent]2[/accent]  Status — preview what export would do (incremental diff)")
        console.print("  [accent]3[/accent]  Convert exported HTML → PDF/DOCX")
        console.print("  [accent]4[/accent]  Merge PDFs into consolidated volumes")
        console.print("  [accent]5[/accent]  Diagnose (check engines + connection)")
        console.print("  [accent]6[/accent]  Initialize or edit config file")
        console.print("  [accent]q[/accent]  Quit")
        choice = ask("Your choice", default="1", choices=["1", "2", "3", "4", "5", "6", "q"])
        if choice == "q":
            break
        if choice == "1":
            export_cmd(ctx)
        elif choice == "2":
            status_cmd(ctx)
        elif choice == "3":
            convert_cmd(ctx)
        elif choice == "4":
            merge_cmd(ctx)
        elif choice == "5":
            diagnose(ctx)
        elif choice == "6":
            init_config(ctx)


# ---------------------------------------------------------------------------
# Subcommand: export
# ---------------------------------------------------------------------------


@app.command("export")
def export_cmd(
    ctx: typer.Context,
    space: str | None = typer.Option(None, help="Space key (overrides config)"),
    output: Path | None = typer.Option(None, help="Output directory (overrides config)"),
    format: str | None = typer.Option(None, help="pdf | docx | md | html"),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", "-y",
        help="Use config as-is without prompting.",
    ),
) -> None:
    """Download a Confluence space to local files."""
    cfg = _load(ctx)
    if space:
        cfg.confluence.space_key = space
    if output:
        cfg.export.output_path = str(output)
    if format:
        cfg.export.format = format

    if not non_interactive:
        section("Confluence Exporter — Export")
        _interactive_confluence_target(cfg)
        _interactive_auth(cfg)

        cfg.export.format = ask(
            "Output format",
            default=cfg.export.format,
            choices=["pdf", "docx", "md", "html"],
        )
        cfg.export.output_path = ask(
            "Output folder", default=cfg.export.output_path
        )
        cfg.export.include_attachments = ask_yes_no(
            "Download attachments?", default=cfg.export.include_attachments
        )
        cfg.export.skip_unchanged = ask_yes_no(
            "Skip pages unchanged since last run?", default=cfg.export.skip_unchanged
        )
        if ask_yes_no("Save these choices to config?", default=True):
            _save(ctx, cfg)
            ok(f"Saved to {ctx.obj['config_path']}")
        if not _confirm_credentials(cfg):
            raise typer.Exit(1)

    errors = cfg.validate()
    if errors:
        for e in errors:
            error(e)
        raise typer.Exit(1)

    summary_table("Export plan", {
        "Base URL": cfg.confluence.base_url,
        "Space":    cfg.confluence.space_key,
        "Format":   cfg.export.format.upper(),
        "Output":   cfg.export.output_path,
        "Auth":     cfg.confluence.auth_mode,
    })
    if not non_interactive and not ask_yes_no("Proceed?", default=True):
        return

    client = ConfluenceClient.from_config(
        cfg.confluence, request_delay_seconds=cfg.export.request_delay_seconds
    )
    _run_export(cfg, client)


def _run_export(cfg: AppConfig, client: ConfluenceClient) -> None:
    with progress_bar("Exporting pages", total=1) as (progress, task_id):
        def cb(title: str, i: int, total: int) -> None:
            progress.update(task_id, total=total, completed=i,
                            description=f"[accent]{title[:70]}[/accent]")

        exporter = SpaceExporter(cfg, client, progress=cb)
        result = exporter.run()

    summary_table("Export summary", {
        "Pages new":           str(result.new_count),
        "Pages updated":       str(result.updated_count),
        "Pages unchanged":     str(result.unchanged_count),
        "Pages failed":        str(result.failed_count),
        "Deleted upstream":    str(result.deleted_upstream),
        "Output folder":       str(Path(cfg.export.output_path).resolve()),
    })
    if result.failed_count:
        warn("Some pages failed — check the log above.")
    elif result.new_count == 0 and result.updated_count == 0:
        ok("Already up to date — nothing to download.")
    else:
        ok(f"Export finished: {result.new_count} new, {result.updated_count} updated.")


# ---------------------------------------------------------------------------
# Subcommand: convert
# ---------------------------------------------------------------------------


@app.command("convert")
def convert_cmd(
    ctx: typer.Context,
    source: Path | None = typer.Argument(None, help="Folder with exported HTML files"),
    format: str = typer.Option("pdf", "--format", "-f", help="pdf | docx"),
    engine: str = typer.Option("auto", "--engine", "-e",
                               help="auto | playwright | weasyprint | xhtml2pdf"),
    merge_pdf_attachments: bool = typer.Option(
        True, "--merge/--no-merge",
        help="Merge PDF attachments into the final document."
    ),
    non_interactive: bool = typer.Option(False, "--non-interactive", "-y"),
) -> None:
    """Convert a folder of exported HTML into clean PDFs/DOCX."""
    cfg = _load(ctx)

    if source is None and not non_interactive:
        section("Convert — HTML → PDF/DOCX")
        src = ask(
            "Source folder with .html files",
            default=cfg.export.output_path,
        )
        source = Path(src)
        format = ask("Target format", default=format, choices=["pdf", "docx"])
        if format == "pdf":
            detected = detect_engine("auto")
            info(f"Detected best engine: [primary]{detected}[/primary]")
            engine = ask(
                "PDF engine",
                default=engine,
                choices=list(engine_names()) + ["auto"],
            )
            merge_pdf_attachments = ask_yes_no(
                "Merge PDF attachments into each output document?",
                default=merge_pdf_attachments,
            )
    elif source is None:
        source = Path(cfg.export.output_path)

    converter = OutputConverter(
        output_root=source,
        target_format=format,
        append_attachment_list=cfg.convert.append_attachment_list,
        engine=engine,
        merge_pdf_attachments=merge_pdf_attachments,
    )

    summary_table("Conversion plan", {
        "Source":            str(source.resolve()),
        "Destination":       str(converter.converted_root.resolve()),
        "Format":            format.upper(),
        "Engine":            engine,
        "Merge PDF attach.": str(merge_pdf_attachments),
    })
    if not non_interactive and not ask_yes_no("Proceed?", default=True):
        return

    with progress_bar("Converting", total=1) as (progress, task_id):
        def cb(name: str, i: int, total: int) -> None:
            progress.update(task_id, total=total, completed=i,
                            description=f"[accent]{name[:70]}[/accent]")

        converter._progress = cb  # inject progress callback
        ok_n, fail_n = converter.run()

    summary_table("Conversion summary", {
        "Succeeded": str(ok_n),
        "Failed":    str(fail_n),
        "Output":    str(converter.converted_root.resolve()),
    })


# ---------------------------------------------------------------------------
# Subcommand: merge
# ---------------------------------------------------------------------------


@app.command("merge")
def merge_cmd(
    ctx: typer.Context,
    source: Path | None = typer.Argument(None, help="Folder with per-page PDFs"),
    destination: Path | None = typer.Argument(None, help="Where to write merged volumes"),
    mode: str = typer.Option(
        "per_section", "--mode", "-m",
        help="per_section | per_space | single",
    ),
    engine: str = typer.Option("auto", "--engine", "-e"),
    non_interactive: bool = typer.Option(False, "--non-interactive", "-y"),
) -> None:
    """Consolidate per-page PDFs into volumes with TOC + bookmark outlines."""
    cfg = _load(ctx)

    if source is None and not non_interactive:
        section("Merge — build consolidated volumes")
        default_src = cfg.export.output_path.rstrip("/\\") + "_converted"
        source = Path(ask("Source folder (per-page PDFs)", default=default_src))
        destination = destination or Path(
            ask(
                "Destination folder (merged volumes)",
                default=cfg.export.output_path.rstrip("/\\") + "_volumes",
            )
        )
        info("Merge modes:")
        console.print("  [accent]1[/accent]  per_section — one PDF per top-level folder (NotebookLM-friendly)")
        console.print("  [accent]2[/accent]  per_space   — one PDF per space folder")
        console.print("  [accent]3[/accent]  single      — one PDF for everything")
        m = ask("Mode", default="1", choices=["1", "2", "3"])
        mode = {"1": "per_section", "2": "per_space", "3": "single"}[m]
        engine = ask(
            "TOC-page engine",
            default=engine,
            choices=list(engine_names()) + ["auto"],
        )
    elif source is None:
        source = Path(cfg.export.output_path.rstrip("/\\") + "_converted")

    if destination is None:
        destination = Path(cfg.merge.destination)

    merger = PDFMerger(
        source_root=source,
        dest_root=destination,
        mode=mode,
        engine=engine,
    )

    summary_table("Merge plan", {
        "Source":      str(source.resolve()),
        "Destination": str(destination.resolve()),
        "Mode":        mode,
        "Engine":      engine,
    })
    if not non_interactive and not ask_yes_no("Proceed?", default=True):
        return

    ok_n, fail_n = merger.run()
    summary_table("Merge summary", {
        "Volumes built": str(ok_n),
        "Failures":      str(fail_n),
        "Output":        str(destination.resolve()),
    })


# ---------------------------------------------------------------------------
# Subcommand: status — preview what an export would do
# ---------------------------------------------------------------------------


@app.command("status")
def status_cmd(
    ctx: typer.Context,
    show_titles: bool = typer.Option(
        False, "--titles/--no-titles",
        help="List the actual page titles in each bucket (can be long).",
    ),
    limit: int = typer.Option(
        20, "--limit", "-n",
        help="Max titles per bucket when --titles is on.",
    ),
) -> None:
    """Preview what a re-run of `export` would do — without downloading anything.

    Compares the live Confluence space against your local lockfile + on-disk
    files and shows how many pages are NEW, UPDATED, UNCHANGED, or DELETED
    upstream. Useful for incremental update planning.
    """
    cfg = _load(ctx)
    errors = cfg.validate()
    if errors:
        for e in errors:
            error(e)
        raise typer.Exit(1)

    section("Status — incremental diff")

    client = ConfluenceClient.from_config(
        cfg.confluence, request_delay_seconds=cfg.export.request_delay_seconds
    )
    try:
        client.test_connection()
    except ConfluenceError as e:
        error(str(e))
        raise typer.Exit(1) from e

    info("Listing pages and comparing to local state…")
    exporter = SpaceExporter(cfg, client)
    diff = exporter.compute_diff()
    s = diff.summary()

    summary_table("Diff", {
        "[success]New[/success]":          str(s["new"]),
        "[warning]Updated[/warning]":      str(s["updated"]),
        "[muted]Unchanged[/muted]":        str(s["unchanged"]),
        "[error]Deleted upstream[/error]": str(s["deleted"]),
        "Total in Confluence":             str(diff.total_remote),
    })

    if show_titles:
        if diff.new:
            info("Pages that are NEW:")
            for p in diff.new[:limit]:
                console.print(f"  [success]+[/success] {p.get('title', '?')}")
            if len(diff.new) > limit:
                console.print(f"  [muted]… and {len(diff.new) - limit} more[/muted]")
        if diff.updated:
            info("Pages that have been UPDATED upstream:")
            for p in diff.updated[:limit]:
                console.print(f"  [warning]~[/warning] {p.get('title', '?')}")
            if len(diff.updated) > limit:
                console.print(f"  [muted]… and {len(diff.updated) - limit} more[/muted]")
        if diff.deleted_ids:
            info("Pages DELETED upstream (still in your lockfile):")
            for pid in diff.deleted_ids[:limit]:
                entry = exporter._lockfile._data.get(pid, {})
                console.print(f"  [error]-[/error] page id {pid}  ({entry.get('path', '?')})")

    if s["new"] == 0 and s["updated"] == 0:
        ok("Already up to date — running `export` would be a no-op.")
    else:
        info(
            f"Running [primary]export[/primary] would download "
            f"[success]{s['new']}[/success] new + "
            f"[warning]{s['updated']}[/warning] updated page(s)."
        )


# ---------------------------------------------------------------------------
# Subcommand: diagnose
# ---------------------------------------------------------------------------


@app.command("diagnose")
def diagnose(ctx: typer.Context) -> None:
    """Check installed PDF engines and test Confluence connectivity."""
    section("Diagnose")

    from confluence_exporter.pdf_engines import _ENGINES

    rows: dict[str, str] = {}
    for name, engine in _ENGINES.items():
        avail = engine.is_available()
        rows[name] = "[success]available[/success]" if avail else (
            f"[warning]missing[/warning]  →  {engine.explain_unavailable()}"
        )
    summary_table("PDF engines", rows)

    best = detect_engine("auto")
    if best == "none":
        error("No PDF engine is usable. Install at least one (playwright recommended).")
    else:
        ok(f"Best available engine: [primary]{best}[/primary]")

    # Connection check (only if config exists)
    cfg_path = ctx.obj["config_path"]
    if Path(cfg_path).exists():
        cfg = _load(ctx)
        if cfg.confluence.base_url and cfg.confluence.auth_mode:
            _confirm_credentials(cfg)
    else:
        warn(f"No config file at {cfg_path} — skipping connection check.")


# ---------------------------------------------------------------------------
# Subcommand: init-config
# ---------------------------------------------------------------------------


@app.command("init-config")
def init_config(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Overwrite existing file"),
) -> None:
    """Create / edit the config file interactively."""
    path = ctx.obj["config_path"]
    exists = Path(path).exists()
    cfg = _load(ctx) if exists else AppConfig()

    banner(subtitle="Config setup")
    if exists and not force:
        info(f"Editing existing config at [primary]{path}[/primary]")
    else:
        info(f"Creating new config at [primary]{path}[/primary]")

    _interactive_confluence_target(cfg)
    _interactive_auth(cfg)

    cfg.export.format = ask(
        "Default export format", default=cfg.export.format,
        choices=["pdf", "docx", "md", "html"],
    )
    cfg.export.output_path = ask(
        "Default output folder", default=cfg.export.output_path
    )

    _save(ctx, cfg)
    ok(f"Saved {path}")

    if ask_yes_no("Test the credentials now?", default=True):
        _confirm_credentials(cfg)


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def main() -> None:  # pragma: no cover — thin wrapper for the console script
    try:
        app()
    except KeyboardInterrupt:
        console.print()
        warn("Interrupted by user")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
