"""Filename sanitation helpers."""

from __future__ import annotations

import hashlib
import re

# Characters that Windows / POSIX filesystems don't allow or that cause pain.
DEFAULT_UNSAFE_MAP: dict[str, str] = {
    "<": "_",
    ">": "_",
    ":": "_",
    '"': "_",
    "/": "_",
    "\\": "_",
    "|": "_",
    "?": "_",
    "*": "_",
    "\x00": "_",
    "[": "_",
    "]": "_",
    "'": "_",
    "\u2019": "_",  # right single quote
    "\u00b4": "_",  # acute accent
    "`": "_",
}


def sanitize_filename(
    name: str,
    encoding_map: dict[str, str] | None = None,
    max_length: int = 200,
    lowercase: bool = False,
) -> str:
    """Return a safe filename.

    Collapses whitespace, substitutes unsafe characters, and — if the result is
    longer than ``max_length`` — truncates and appends a short hash to stay
    unique.
    """
    if not name:
        return "_"
    encoding_map = encoding_map if encoding_map is not None else DEFAULT_UNSAFE_MAP

    # Apply user-supplied character substitutions
    for bad, good in encoding_map.items():
        name = name.replace(bad, good)

    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip().strip(".")

    if lowercase:
        name = name.lower()

    if max_length and len(name) > max_length:
        digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
        keep = max(1, max_length - 9)  # 8 for hash + 1 for underscore
        name = name[:keep].rstrip() + "_" + digest

    return name or "_"


def short_section_name(name: str, max_length: int = 150) -> str:
    """Sanitize a name for use as a folder / output file name (stricter)."""
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    return name[:max_length] or "_"
