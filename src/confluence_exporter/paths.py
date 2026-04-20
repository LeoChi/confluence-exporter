"""Windows long-path helpers and safe file operations.

Windows caps regular paths at 260 characters (MAX_PATH). Deeply-nested
Confluence hierarchies routinely exceed this. Prefixing an absolute path
with ``\\\\?\\`` bypasses the limit for most Python file APIs.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def long_path(p: str | Path) -> str:
    r"""On Windows, prefix an absolute path with ``\\?\`` to bypass MAX_PATH.

    No-op on non-Windows platforms.
    """
    p = str(p)
    if os.name != "nt":
        return p
    abs_p = os.path.abspath(p)
    if abs_p.startswith("\\\\?\\"):
        return abs_p
    if abs_p.startswith("\\\\"):
        # UNC path: \\server\share\... -> \\?\UNC\server\share\...
        return "\\\\?\\UNC\\" + abs_p[2:]
    return "\\\\?\\" + abs_p


def safe_makedirs(path: str | Path) -> None:
    """``os.makedirs`` that tolerates long Windows paths."""
    try:
        os.makedirs(long_path(path), exist_ok=True)
    except OSError:
        # Fall back to a plain attempt so errors still propagate meaningfully
        os.makedirs(str(path), exist_ok=True)


def safe_write_bytes(target: str | Path, data: bytes) -> bool:
    """Write bytes to a possibly-long path. Returns success."""
    target = str(target)
    parent = os.path.dirname(target) or "."
    try:
        safe_makedirs(parent)
        with open(long_path(target), "wb") as fh:
            fh.write(data)
        return True
    except OSError:
        return False


def safe_read_bytes(path: str | Path) -> bytes | None:
    try:
        with open(long_path(path), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def move_into_place(src: str | Path, dst: str | Path) -> bool:
    """Move *src* to *dst*, tolerating long destination paths.

    Falls back to copy+delete when os.replace raises (e.g. cross-device).
    """
    src = str(src)
    dst = str(dst)
    try:
        safe_makedirs(os.path.dirname(dst) or ".")
    except OSError:
        pass
    try:
        os.replace(long_path(src), long_path(dst))
        return True
    except OSError:
        try:
            shutil.copyfile(long_path(src), long_path(dst))
            try:
                os.remove(long_path(src))
            except OSError:
                pass
            return True
        except OSError:
            return False


def is_valid_pdf(path: str | Path, min_size: int = 1024) -> bool:
    """A file is a valid PDF if it exists, is > ``min_size`` bytes, and starts
    with ``%PDF-``."""
    try:
        lp = long_path(path)
        st = os.stat(lp)
        if st.st_size < min_size:
            return False
        with open(lp, "rb") as fh:
            return fh.read(5).startswith(b"%PDF-")
    except OSError:
        return False


def resolve_under(root: str | Path, *parts: str) -> Path:
    """Concatenate *parts* under *root*, returning a Path (long-path-safe
    stringify with :func:`long_path` when needed)."""
    return Path(root).joinpath(*parts)
