"""Typed configuration — loading, defaults, validation, persistence.

The on-disk format is JSON (see ``examples/config.example.json``). At runtime
config is exposed as the :class:`AppConfig` dataclass tree so the rest of the
codebase can use attribute access.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from confluence_exporter.filename import DEFAULT_UNSAFE_MAP

DEFAULT_CONFIG_PATH = "config.json"

# ---------------------------------------------------------------------------
# Dataclass tree
# ---------------------------------------------------------------------------


@dataclass
class ConfluenceConfig:
    base_url: str = ""
    space_key: str = ""
    auth_mode: str = "api_token"  # api_token | pat | browser_cookie
    email: str = ""
    api_token: str = ""
    personal_access_token: str = ""
    cookies: dict[str, str] = field(default_factory=dict)


@dataclass
class ExportConfig:
    format: str = "pdf"  # pdf | docx | md | html
    output_path: str = "./output"
    include_attachments: bool = True
    include_gliffy: bool = True
    page_path: str = "{space_name}/{ancestor_titles}/{page_title}"
    attachment_path: str = "{space_name}/attachments/{page_title}/{attachment_filename}"
    filename_encoding: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_UNSAFE_MAP)
    )
    filename_max_length: int = 200
    filename_lowercase: bool = False
    include_document_title: bool = True
    page_breadcrumbs: bool = True
    skip_unchanged: bool = True
    cleanup_stale: bool = False
    lockfile_name: str = "confluence-lock.json"
    batch_size: int = 25
    request_delay_seconds: float = 0.25
    log_level: str = "INFO"


@dataclass
class ConvertConfig:
    engine: str = "auto"  # auto | playwright | weasyprint | xhtml2pdf
    append_attachment_list: bool = True
    merge_pdf_attachments: bool = True


@dataclass
class MergeConfig:
    mode: str = "per_section"  # per_section | per_space | single
    destination: str = "./output_volumes"


@dataclass
class AppConfig:
    confluence: ConfluenceConfig = field(default_factory=ConfluenceConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    convert: ConvertConfig = field(default_factory=ConvertConfig)
    merge: MergeConfig = field(default_factory=MergeConfig)

    # ----- serialization -----
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        # Tolerate missing sections and unknown keys (forward-compat)
        def _pick(dc, raw: dict[str, Any]):
            if not raw:
                return dc()
            valid = {f for f in dc.__dataclass_fields__}
            return dc(**{k: v for k, v in raw.items() if k in valid})

        return cls(
            confluence=_pick(ConfluenceConfig, data.get("confluence", {}) or {}),
            export=_pick(ExportConfig, data.get("export", {}) or {}),
            convert=_pick(ConvertConfig, data.get("convert", {}) or {}),
            merge=_pick(MergeConfig, data.get("merge", {}) or {}),
        )

    def validate(self) -> list[str]:
        errs: list[str] = []
        c = self.confluence
        if not c.base_url:
            errs.append("confluence.base_url is required (e.g. https://your-tenant.atlassian.net)")
        if not c.space_key:
            errs.append("confluence.space_key is required")

        if c.auth_mode == "api_token":
            if not c.email:
                errs.append("confluence.email is required for api_token auth")
            if not c.api_token:
                errs.append("confluence.api_token is required for api_token auth")
        elif c.auth_mode == "pat":
            if not c.personal_access_token:
                errs.append("confluence.personal_access_token is required for pat auth")
        elif c.auth_mode == "browser_cookie":
            if not c.cookies:
                errs.append("confluence.cookies must contain at least one entry for browser_cookie auth")
        else:
            errs.append(
                f"Unknown confluence.auth_mode: {c.auth_mode!r} "
                "(expected: api_token | pat | browser_cookie)"
            )

        if self.export.format not in ("pdf", "docx", "md", "html"):
            errs.append(f"Unknown export.format: {self.export.format!r}")

        if self.convert.engine not in ("auto", "playwright", "weasyprint", "xhtml2pdf"):
            errs.append(f"Unknown convert.engine: {self.convert.engine!r}")

        if self.merge.mode not in ("per_section", "per_space", "single"):
            errs.append(f"Unknown merge.mode: {self.merge.mode!r}")

        return errs


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Load a config file, or return defaults if the file doesn't exist."""
    path = Path(path)
    if not path.exists():
        return AppConfig()
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    # strip fields beginning with "_" — those are documentation comments
    return AppConfig.from_dict(_strip_comment_keys(data))


def save_config(config: AppConfig, path: str | Path = DEFAULT_CONFIG_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(config.to_dict(), fh, indent=2, ensure_ascii=False)


def _strip_comment_keys(obj: Any) -> Any:
    """Recursively drop keys that start with ``_`` (user-facing docs)."""
    if isinstance(obj, dict):
        return {k: _strip_comment_keys(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_comment_keys(x) for x in obj]
    return obj
