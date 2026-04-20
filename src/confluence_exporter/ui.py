"""Rich-based interactive UI primitives (Facade).

Every user-facing prompt, banner, table and progress bar in the CLI goes
through this module so the look-and-feel stays consistent.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterable

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

THEME = Theme({
    "primary": "bold cyan",
    "secondary": "magenta",
    "success": "bold green",
    "warning": "bold yellow",
    "danger": "bold red",
    "muted": "dim",
    "accent": "bold blue",
})

console = Console(theme=THEME, highlight=False)


def banner(subtitle: str = "") -> None:
    title = Text("Confluence Exporter", style="primary")
    inner = Text()
    inner.append("Export ", style="muted")
    inner.append("·", style="accent")
    inner.append(" Convert ", style="muted")
    inner.append("·", style="accent")
    inner.append(" Merge\n", style="muted")
    inner.append(subtitle, style="secondary")
    console.print(
        Panel(inner, title=title, border_style="accent", padding=(0, 2))
    )


def section(title: str) -> None:
    console.rule(f"[primary]{title}[/primary]", style="accent")


def ok(msg: str) -> None:
    console.print(f"[success]✓[/success] {msg}")


def info(msg: str) -> None:
    console.print(f"[accent]•[/accent] {msg}")


def warn(msg: str) -> None:
    console.print(f"[warning]![/warning] {msg}")


def error(msg: str) -> None:
    console.print(f"[danger]✗[/danger] {msg}")


# -------- Prompts ---------------------------------------------------------


def ask(label: str, default: str | None = None, password: bool = False,
        choices: Iterable[str] | None = None) -> str:
    return Prompt.ask(
        f"[primary]{label}[/primary]",
        default=default,
        password=password,
        choices=list(choices) if choices else None,
        show_default=default not in (None, ""),
        show_choices=bool(choices),
        console=console,
    )


def ask_yes_no(label: str, default: bool = True) -> bool:
    return Confirm.ask(
        f"[primary]{label}[/primary]",
        default=default,
        console=console,
    )


def ask_multiline(label: str, end_marker: str = "END") -> str:
    info(f"{label}  (end with a line containing just '{end_marker}')")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == end_marker:
            break
        lines.append(line)
    return "\n".join(lines)


# -------- Progress bars / tables -----------------------------------------


def make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("/"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )


@contextmanager
def progress_bar(description: str, total: int):
    """Yields a ``(progress, task_id)`` pair."""
    p = make_progress()
    with p:
        task_id = p.add_task(description, total=total)
        yield p, task_id


def summary_table(title: str, rows: dict[str, str]) -> None:
    table = Table(title=title, border_style="accent", show_header=False)
    table.add_column("Key", style="muted")
    table.add_column("Value", style="primary")
    for k, v in rows.items():
        table.add_row(k, v)
    console.print(table)


def print_spaces_table(spaces: list[dict]) -> None:
    """Display a table of Confluence spaces (from list_spaces)."""
    table = Table(title="Confluence spaces", border_style="accent")
    table.add_column("#", style="muted", no_wrap=True)
    table.add_column("Key", style="accent")
    table.add_column("Name", style="primary")
    table.add_column("Type", style="muted")
    for i, s in enumerate(spaces, 1):
        table.add_row(
            str(i),
            s.get("key", ""),
            s.get("name", ""),
            s.get("type", ""),
        )
    console.print(table)
