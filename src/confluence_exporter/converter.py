"""Convert a tree of exported HTML files into PDFs/DOCX.

This operates on an already-exported folder (typically the output of
:class:`~confluence_exporter.exporter.SpaceExporter` with HTML format). It
cleans the HTML, embeds local attachments, renders to the target format, and
optionally merges PDF attachments as appendix pages.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from confluence_exporter.filename import short_section_name
from confluence_exporter.formatters import DocxFormatter
from confluence_exporter.html_cleaner import clean_confluence_html
from confluence_exporter.logging_utils import get_logger
from confluence_exporter.paths import (
    is_valid_pdf,
    long_path,
    move_into_place,
    safe_makedirs,
)
from confluence_exporter.pdf_engines import (
    detect_engine,
    render_html_to_pdf,
    shutdown_engines,
)

logger = get_logger()

ProgressCallback = Callable[[str, int, int], None] | None


# ---------------------------------------------------------------------------
# PDF-attachment merge (pypdf)
# ---------------------------------------------------------------------------


def merge_pdf_with_attachments(
    main_pdf: str, attachment_pdfs: list[str], output: str
) -> bool:
    """Append PDF attachments to ``main_pdf`` and write the result to ``output``.

    Always writes through a temp file so Windows long paths work.
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        logger.debug("pypdf not installed — leaving main PDF as-is")
        if main_pdf != output:
            move_into_place(main_pdf, output) or shutil.copyfile(
                long_path(main_pdf), long_path(output)
            )
        return False

    fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="cfx_merge_")
    os.close(fd)
    writer = PdfWriter()
    try:
        for p in PdfReader(long_path(main_pdf)).pages:
            writer.add_page(p)
        for att in attachment_pdfs:
            try:
                for p in PdfReader(long_path(att)).pages:
                    writer.add_page(p)
            except Exception as e:
                logger.warning("  Could not merge '%s': %s", os.path.basename(att), e)
        with open(tmp, "wb") as fh:
            writer.write(fh)
        if not is_valid_pdf(tmp):
            return False
        return move_into_place(tmp, output)
    except Exception as e:
        logger.warning("  PDF merge failed: %s", e)
        return False
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# OutputConverter
# ---------------------------------------------------------------------------


class OutputConverter:
    """Scan a folder of ``.html`` files and produce clean ``.pdf`` / ``.docx``."""

    def __init__(
        self,
        output_root: Path,
        target_format: str,
        *,
        converted_dir_suffix: str = "_converted",
        append_attachment_list: bool = True,
        engine: str = "auto",
        merge_pdf_attachments: bool = True,
        progress: ProgressCallback = None,
    ):
        self.output_root = Path(output_root)
        self.target_format = target_format
        self.converted_root = self.output_root.parent / (
            self.output_root.name + converted_dir_suffix
        )
        self.append_attachment_list = append_attachment_list
        self.engine = engine
        self.merge_pdf_attachments = merge_pdf_attachments
        self._progress = progress

        self._attachments_by_title: dict[str, Path] = {}
        self._attachments_by_pageid: dict[str, Path] = {}
        self._scan_attachment_dirs()

    # ------------------------------------------------------------------
    def _scan_attachment_dirs(self) -> None:
        if not self.output_root.exists():
            return
        for space_dir in self.output_root.iterdir():
            if not space_dir.is_dir():
                continue
            att_root = space_dir / "attachments"
            if not att_root.exists():
                continue
            for sub in att_root.iterdir():
                if sub.is_dir() and sub.name != "_flat":
                    self._attachments_by_title[sub.name] = sub
            flat = att_root / "_flat"
            if flat.exists():
                for f in flat.iterdir():
                    if f.is_file():
                        parts = f.name.split("_", 1)
                        if len(parts) == 2 and parts[0].isdigit():
                            self._attachments_by_pageid.setdefault(parts[0], flat)
        logger.debug(
            "Indexed %d attachment folders (by title) + %d page-id buckets",
            len(self._attachments_by_title), len(self._attachments_by_pageid),
        )

    def _attachments_for(self, html_path: Path) -> dict[str, str]:
        stem = html_path.stem
        if html_path.parent.name == "_flat":
            m = re.search(r"_(\d+)$", stem)
            if m:
                flat = self._attachments_by_pageid.get(m.group(1))
                if flat:
                    files: dict[str, str] = {}
                    prefix = f"{m.group(1)}_"
                    for f in flat.iterdir():
                        if f.is_file() and f.name.startswith(prefix):
                            files[f.name[len(prefix):]] = str(f.resolve())
                    return files
            return {}
        d = self._attachments_by_title.get(stem)
        if d:
            return {f.name: str(f.resolve()) for f in d.iterdir() if f.is_file()}
        return {}

    # ------------------------------------------------------------------
    def _mirror_output_path(self, html_path: Path) -> Path:
        rel = html_path.relative_to(self.output_root)
        ext = ".pdf" if self.target_format == "pdf" else ".docx"
        candidate = self.converted_root / rel.with_suffix(ext)
        # Windows long-path safety
        if os.name == "nt" and len(str(candidate.resolve())) > 240:
            parts = rel.parts
            space_bucket = parts[0] if len(parts) > 1 else "_root"
            flat_stem = "_".join(parts[1:]) if len(parts) > 1 else rel.stem
            flat_stem = flat_stem.rsplit(".", 1)[0]
            flat_stem = short_section_name(flat_stem, max_length=180)
            return self.converted_root / space_bucket / "_flat" / (flat_stem + ext)
        return candidate

    def _wrap_html(
        self, page_title: str, cleaned_body: str, attachments: dict[str, str]
    ) -> str:
        parts = [
            "<!DOCTYPE html><html><head><meta charset='utf-8'>",
            f"<title>{page_title}</title>",
            "<style>",
            "body{font-family:Helvetica,Arial,sans-serif;font-size:10pt;}",
            "h1{font-size:18pt;color:#172B4D;}",
            "h2{font-size:14pt;color:#172B4D;}",
            "h3{font-size:12pt;color:#42526E;}",
            "pre{background:#F4F5F7;padding:8px;font-size:9pt;}",
            "code{background:#F4F5F7;padding:1px 4px;}",
            "table{border-collapse:collapse;margin:8px 0;}",
            "th,td{border:1px solid #DFE1E6;padding:4px 8px;}",
            "th{background:#F4F5F7;}",
            "img{max-width:100%;}",
            ".attachments{margin-top:20pt;padding-top:10pt;"
            "border-top:1px solid #DFE1E6;}",
            ".attachments h2{font-size:12pt;}",
            "</style></head><body>",
            f"<h1>{page_title}</h1>",
            cleaned_body,
        ]
        if self.append_attachment_list and attachments:
            non_images = {
                k: v for k, v in attachments.items()
                if not k.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"))
            }
            if non_images:
                parts.append("<div class='attachments'><h2>Attachments</h2><ul>")
                for name, path in sorted(non_images.items()):
                    parts.append(f"<li><a href='{path}'>{name}</a></li>")
                parts.append("</ul></div>")
        parts.append("</body></html>")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    def run(self) -> tuple[int, int]:
        html_files = sorted(self.output_root.rglob("*.html"))
        total = len(html_files)
        if total == 0:
            logger.warning("No .html files found in %s", self.output_root)
            return 0, 0

        real_engine = detect_engine(self.engine) if self.target_format == "pdf" else "n/a"
        logger.info("Source: %s", self.output_root.resolve())
        logger.info("Dest:   %s", self.converted_root.resolve())
        logger.info("Format: %s", self.target_format.upper())
        if self.target_format == "pdf":
            logger.info("Engine: %s (requested: %s)", real_engine, self.engine)
            logger.info("Merge PDF attachments: %s", self.merge_pdf_attachments)

        safe_makedirs(self.converted_root)

        ok = 0
        fail = 0
        engine_stats: dict[str, int] = {}
        merged_count = 0
        docx_fmt = DocxFormatter()

        try:
            for i, html_path in enumerate(html_files, 1):
                if self._progress:
                    self._progress(html_path.name, i, total)
                try:
                    out_path = self._mirror_output_path(html_path)
                    safe_makedirs(out_path.parent)
                    raw = html_path.read_text(encoding="utf-8", errors="replace")
                    attachments = self._attachments_for(html_path)
                    cleaned = clean_confluence_html(raw, attachments)
                    full_html = self._wrap_html(html_path.stem, cleaned, attachments)

                    if self.target_format == "pdf":
                        pdf_attachments = [
                            p for n, p in attachments.items()
                            if n.lower().endswith(".pdf")
                        ] if self.merge_pdf_attachments else []

                        tmp_main = str(out_path) + ".main.pdf" if pdf_attachments else str(out_path)

                        success, used = render_html_to_pdf(
                            full_html, tmp_main, preference=self.engine
                        )
                        if not success:
                            raise RuntimeError(f"PDF render failed: {used}")
                        engine_stats[used] = engine_stats.get(used, 0) + 1

                        if pdf_attachments:
                            merged = merge_pdf_with_attachments(
                                tmp_main, pdf_attachments, str(out_path)
                            )
                            if merged:
                                merged_count += 1
                            else:
                                move_into_place(tmp_main, str(out_path))
                            with contextlib.suppress(OSError):
                                os.remove(tmp_main)

                        if not is_valid_pdf(str(out_path)):
                            raise RuntimeError("Resulting PDF is invalid")
                    elif self.target_format == "docx":
                        docx_fmt.write(
                            html_body=cleaned,
                            output_path=str(out_path),
                            page_title=html_path.stem,
                            breadcrumbs="",
                        )
                    else:
                        raise ValueError(f"Unsupported format: {self.target_format}")

                    ok += 1
                except Exception as e:
                    fail += 1
                    logger.error("  ! %s: %s", html_path.name, e)
                    # Clean up 0-byte leftover
                    try:
                        if (
                            self.target_format == "pdf"
                            and "out_path" in locals()
                            and os.path.exists(str(out_path))
                            and not is_valid_pdf(str(out_path))
                        ):
                            os.remove(str(out_path))
                    except OSError:
                        pass
        finally:
            if self.target_format == "pdf":
                shutdown_engines()

        logger.info("Converted %d OK, %d failed", ok, fail)
        if engine_stats:
            logger.info(
                "Engines used: %s",
                ", ".join(f"{k}={v}" for k, v in engine_stats.items()),
            )
        if self.target_format == "pdf" and self.merge_pdf_attachments:
            logger.info("Docs with merged PDF appendices: %d", merged_count)
        return ok, fail
