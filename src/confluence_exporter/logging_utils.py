"""Logging setup. Uses RichHandler so log messages blend with the UI."""

from __future__ import annotations

import logging

from rich.logging import RichHandler

_LOGGER_NAME = "confluence_exporter"


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure the package logger to use Rich."""
    lvl = getattr(logging, level.upper(), logging.INFO)

    handler = RichHandler(
        show_time=True,
        show_path=False,
        rich_tracebacks=True,
        markup=False,
    )
    handler.setLevel(lvl)

    root = get_logger()
    # Replace any pre-existing handlers to avoid duplicate output
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(lvl)
    root.propagate = False
    return root
