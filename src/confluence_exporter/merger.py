"""Consolidate many per-page PDFs into volumes with TOC + bookmark outlines."""

from __future__ import annotations

import contextlib
import html
import shutil
import tempfile
import threading
from pathlib import Path

from confluence_exporter.filename import short_section_name
from confluence_exporter.logging_utils import get_logger
from confluence_exporter.paths import (
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


def _escape(s: str) -> str:
    return html.escape(s or "", quote=True)


class PDFMerger:
    """Build consolidated volumes from a tree of per-page PDFs.

    Each output PDF contains:

    * A **Table of Contents** page (rendered with the preferred PDF engine).
    * The body pages, in sorted folder order.
    * A **hierarchical PDF outline** (bookmarks) mirroring the folder layout.

    ``mode``
        ``"per_section"``  — one volume per immediate subfolder of each space.
        ``"per_space"``    — one volume per space.
        ``"single"``       — one volume for everything combined.
    """

    def __init__(
        self,
        source_root: Path,
        dest_root: Path,
        *,
        mode: str = "per_section",
        engine: str = "auto",
        cancel_event: threading.Event | None = None,
    ):
        if mode not in ("per_section", "per_space", "single"):
            raise ValueError(f"Unknown mode: {mode}")
        self.source_root = Path(source_root)
        self.dest_root = Path(dest_root)
        self.mode = mode
        self.engine = engine
        self._cancel_event = cancel_event

    def _is_cancelled(self) -> bool:
        return bool(self._cancel_event and self._cancel_event.is_set())

    # ------------------------------------------------------------------
    # Discovery / grouping
    # ------------------------------------------------------------------
    def _collect_entries(self, space_dir: Path) -> list[dict]:
        """Return ordered entries: ``{path, title, hierarchy}``."""
        entries: list[dict] = []
        for pdf in sorted(space_dir.rglob("*.pdf")):
            rel = pdf.relative_to(space_dir)
            parts = rel.parts
            if parts and parts[0] == "_flat":
                continue
            entries.append({
                "path": pdf,
                "title": pdf.stem,
                "hierarchy": tuple(parts[:-1]),
            })

        flat_dir = space_dir / "_flat"
        if flat_dir.exists():
            top_level = {e["hierarchy"][0] for e in entries if e["hierarchy"]}
            for pdf in sorted(flat_dir.glob("*.pdf")):
                stem = pdf.stem
                matched = None
                for sec in sorted(top_level, key=len, reverse=True):
                    if stem == sec or stem.startswith(sec + "_"):
                        matched = sec
                        break
                if matched:
                    remainder = stem[len(matched) + 1:] if stem != matched else matched
                    entries.append({
                        "path": pdf,
                        "title": remainder or matched,
                        "hierarchy": (matched, "(long-path pages)"),
                    })
                else:
                    entries.append({
                        "path": pdf,
                        "title": stem,
                        "hierarchy": ("_flat",),
                    })
        return entries

    # ------------------------------------------------------------------
    # TOC rendering
    # ------------------------------------------------------------------
    def _render_toc_pdf(
        self, rows: list[tuple[str, int | None, int]], label: str, tmp_dir: Path
    ) -> Path | None:
        css = """
        body{font-family:Helvetica,Arial,sans-serif;font-size:10pt;padding:10px 20px;}
        h1{font-size:20pt;color:#172B4D;border-bottom:2px solid #0052CC;
           padding-bottom:6pt;margin-bottom:14pt;}
        .row{display:flex;justify-content:space-between;align-items:baseline;
             margin:2pt 0;padding:2pt 0;border-bottom:1px dotted #DFE1E6;}
        .title{flex:1;padding-right:8pt;}
        .page{color:#5E6C84;font-size:9pt;white-space:nowrap;}
        .d0{font-weight:bold;font-size:12pt;margin-top:12pt;
            color:#0052CC;border-top:1px solid #0052CC;padding-top:6pt;}
        .d0 .page{color:#0052CC;}
        .d1{margin-left:18pt;}
        .d2{margin-left:34pt;font-size:9.5pt;color:#42526E;}
        .d3{margin-left:50pt;font-size:9pt;color:#6B778C;}
        .d4{margin-left:64pt;font-size:9pt;color:#6B778C;}
        .d5{margin-left:78pt;font-size:9pt;color:#8993A4;}
        """
        body = [f"<h1>Table of Contents — {_escape(label)}</h1>"]
        for title, page_no, depth in rows:
            cls = f"d{min(depth, 5)}"
            page_html = f"p. {page_no}" if page_no else ""
            body.append(
                f"<div class='row {cls}'>"
                f"<span class='title'>{_escape(title)}</span>"
                f"<span class='page'>{page_html}</span></div>"
            )
        doc = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<style>{css}</style></head><body>{''.join(body)}</body></html>"
        )
        toc_pdf = tmp_dir / "toc.pdf"
        ok, engine_used = render_html_to_pdf(doc, str(toc_pdf), preference=self.engine)
        if not ok:
            logger.warning("TOC rendering failed (%s)", engine_used)
            return None
        return toc_pdf

    # ------------------------------------------------------------------
    # Build one volume
    # ------------------------------------------------------------------
    def _build_group_pdf(
        self, group_label: str, entries: list[dict], out_path: Path
    ) -> bool:
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            logger.error("pypdf is required for merging. Run: pip install pypdf")
            return False

        tmp_dir = Path(tempfile.mkdtemp(prefix="cfx_vol_"))
        try:
            # Pass 1: build body
            body_writer = PdfWriter()
            records: list[dict] = []
            current_page = 0
            for e in entries:
                try:
                    reader = PdfReader(long_path(str(e["path"])))
                    n = len(reader.pages)
                except Exception as ex:
                    logger.warning("  Skipping unreadable PDF '%s': %s", e["path"].name, ex)
                    continue
                if n == 0:
                    continue
                start = current_page
                for p in reader.pages:
                    body_writer.add_page(p)
                records.append({
                    "title": e["title"],
                    "hierarchy": e["hierarchy"],
                    "depth": len(e["hierarchy"]),
                    "body_start": start,
                    "n_pages": n,
                })
                current_page += n

            if not records:
                logger.warning("  No readable PDFs in group '%s'", group_label)
                return False

            body_tmp = tmp_dir / "body.pdf"
            with open(body_tmp, "wb") as fh:
                body_writer.write(fh)

            # Pass 2: iterate TOC rendering until page count is stable
            def build_toc_rows(offset: int) -> list[tuple]:
                rows: list[tuple] = []
                last_hier: tuple = ()
                for r in records:
                    for i, seg in enumerate(r["hierarchy"]):
                        prefix = r["hierarchy"][:i + 1]
                        if i >= len(last_hier) or last_hier[:i + 1] != prefix:
                            rows.append((seg, None, i))
                    last_hier = r["hierarchy"]
                    page_no = r["body_start"] + offset + 1
                    rows.append((r["title"], page_no, len(r["hierarchy"])))
                return rows

            toc_pdf: Path | None = None
            toc_pages = 1
            for _ in range(4):
                rendered = self._render_toc_pdf(
                    build_toc_rows(offset=toc_pages), group_label, tmp_dir
                )
                if rendered is None:
                    toc_pdf = None
                    toc_pages = 0
                    break
                n = len(PdfReader(long_path(str(rendered))).pages)
                toc_pdf = rendered
                if n == toc_pages:
                    break
                toc_pages = n

            # Pass 3: final assembly with outline
            final_writer = PdfWriter()
            if toc_pdf is not None:
                for p in PdfReader(long_path(str(toc_pdf))).pages:
                    final_writer.add_page(p)
            body_offset = toc_pages if toc_pdf else 0
            for p in PdfReader(long_path(str(body_tmp))).pages:
                final_writer.add_page(p)

            if toc_pdf is not None:
                final_writer.add_outline_item("Table of Contents", 0)

            parent_by_path: dict[tuple, object] = {}
            for r in records:
                parent = None
                for i, seg in enumerate(r["hierarchy"]):
                    key = r["hierarchy"][:i + 1]
                    if key not in parent_by_path:
                        parent_by_path[key] = final_writer.add_outline_item(
                            seg, r["body_start"] + body_offset, parent=parent
                        )
                    parent = parent_by_path[key]
                final_writer.add_outline_item(
                    r["title"], r["body_start"] + body_offset, parent=parent
                )

            safe_makedirs(out_path.parent)
            tmp_final = tmp_dir / "final.pdf"
            with open(tmp_final, "wb") as fh:
                final_writer.write(fh)
            if not move_into_place(str(tmp_final), str(out_path)):
                logger.error("  Could not move final volume to %s", out_path)
                return False

            total = current_page + body_offset
            logger.info(
                "  [OK] %s — %d docs, %d pages (TOC: %d)",
                out_path.name, len(records), total, toc_pages,
            )
            return True
        except Exception as e:
            logger.error("  [FAIL] %s: %s", group_label, e)
            return False
        finally:
            with contextlib.suppress(Exception):
                shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------
    def run(self) -> tuple[int, int]:
        if not self.source_root.exists():
            logger.error("Source folder not found: %s", self.source_root)
            return 0, 0

        space_dirs = sorted(d for d in self.source_root.iterdir() if d.is_dir())
        if not space_dirs:
            logger.error("No space folders under %s", self.source_root)
            return 0, 0

        safe_makedirs(self.dest_root)
        real_engine = detect_engine(self.engine)
        logger.info("Source: %s", self.source_root.resolve())
        logger.info("Dest:   %s", self.dest_root.resolve())
        logger.info("Mode:   %s", self.mode)
        logger.info("Engine: %s (for TOC)", real_engine)

        ok = 0
        fail = 0
        try:
            for space_dir in space_dirs:
                if self._is_cancelled():
                    logger.warning("Merge cancelled by user.")
                    return ok, fail
                logger.info("-- Space: %s --", space_dir.name)
                entries = self._collect_entries(space_dir)
                if not entries:
                    logger.warning("  No PDFs under %s", space_dir)
                    continue

                # Choose grouping
                if self.mode == "per_space":
                    groups = {space_dir.name: entries}
                elif self.mode == "single":
                    groups = {"Confluence Consolidated": entries}
                else:  # per_section
                    groups = {}
                    for e in entries:
                        key = e["hierarchy"][0] if e["hierarchy"] else "_root"
                        groups.setdefault(key, []).append(e)

                logger.info(
                    "  %d PDFs → %d volume(s): %s",
                    len(entries), len(groups), ", ".join(sorted(groups.keys())),
                )

                for group_name, group_entries in groups.items():
                    if self._is_cancelled():
                        logger.warning("Merge cancelled by user.")
                        return ok, fail
                    out_dir = (
                        self.dest_root if self.mode == "single"
                        else self.dest_root / space_dir.name
                    )
                    out_path = out_dir / f"{short_section_name(group_name)}.pdf"
                    label = (
                        f"{space_dir.name} — {group_name}"
                        if group_name != space_dir.name
                        else space_dir.name
                    )
                    if self._build_group_pdf(label, group_entries, out_path):
                        ok += 1
                    else:
                        fail += 1
        finally:
            shutdown_engines()

        logger.info("Merge done: %d volume(s), %d failure(s)", ok, fail)
        logger.info("Output: %s", self.dest_root.resolve())
        return ok, fail
