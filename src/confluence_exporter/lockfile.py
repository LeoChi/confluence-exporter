"""Per-space lockfile used to skip unchanged pages across runs."""

from __future__ import annotations

import json
from pathlib import Path


class Lockfile:
    """JSON-backed registry of ``{page_id: {version, path}}`` entries."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    self._data = json.load(fh) or {}
            except (OSError, json.JSONDecodeError):
                self._data = {}

    # ----- queries -----
    def page_version(self, page_id: str) -> int:
        entry = self._data.get(page_id)
        return int(entry.get("version", 0)) if entry else 0

    def all_page_paths(self) -> set[str]:
        return {v.get("path", "") for v in self._data.values() if v.get("path")}

    # ----- mutations -----
    def set_page(self, page_id: str, version: int, path: str) -> None:
        self._data[page_id] = {"version": int(version), "path": path}

    def forget(self, page_id: str) -> None:
        self._data.pop(page_id, None)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, ensure_ascii=False)
