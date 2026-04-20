"""Download a Confluence space to disk (pages + attachments)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from confluence_exporter.client import ConfluenceClient, ConfluenceError
from confluence_exporter.config import AppConfig
from confluence_exporter.filename import sanitize_filename
from confluence_exporter.formatters import Formatter, build_formatter
from confluence_exporter.html_cleaner import clean_confluence_html
from confluence_exporter.lockfile import Lockfile
from confluence_exporter.logging_utils import get_logger
from confluence_exporter.paths import safe_makedirs, safe_write_bytes

logger = get_logger()

ProgressCallback = Callable[[str, int, int], None] | None


class SpaceExporter:
    """Orchestrates downloading a whole Confluence space to local files.

    A single call to :meth:`run` will:

    1. Test credentials.
    2. List every page in the space.
    3. Skip pages already in the lockfile at the same ``version`` (if enabled).
    4. Download body + attachments.
    5. Write each page via the selected :class:`Formatter`.
    6. Update the lockfile on disk.
    """

    MAX_PATH_HINT = 240  # Windows MAX_PATH (-20 chars for filename extension)

    def __init__(
        self,
        config: AppConfig,
        client: ConfluenceClient,
        *,
        progress: ProgressCallback = None,
    ):
        self.config = config
        self.client = client
        self._progress = progress

        self._formatter: Formatter = build_formatter(
            config.export.format,
            pdf_engine=config.convert.engine,
        )

        output_root = Path(config.export.output_path)
        safe_makedirs(output_root)
        self.output_root = output_root.resolve()

        self._lockfile = Lockfile(output_root / config.export.lockfile_name)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------
    def _sanitize(self, name: str) -> str:
        return sanitize_filename(
            name,
            encoding_map=self.config.export.filename_encoding,
            max_length=self.config.export.filename_max_length,
            lowercase=self.config.export.filename_lowercase,
        )

    def _build_ancestor_path(self, page: dict) -> Path:
        parts = [self._sanitize(a.get("title", "")) for a in page.get("ancestors", [])]
        return Path(*parts) if parts else Path()

    def _build_breadcrumbs(self, page: dict) -> str:
        return " > ".join(a.get("title", "") for a in page.get("ancestors", []))

    def _page_filepath(self, space_name: str, page: dict) -> Path:
        title = self._sanitize(page.get("title", "untitled"))
        ancestors = self._build_ancestor_path(page)
        ext = self._formatter.extension
        candidate = self.output_root / self._sanitize(space_name) / ancestors / f"{title}.{ext}"
        # Windows long-path safety: fall back to _flat when the path gets huge
        if len(str(candidate.resolve())) > self.MAX_PATH_HINT:
            flat_name = f"{title}_{page.get('id', '')}.{ext}"
            return self.output_root / self._sanitize(space_name) / "_flat" / flat_name
        return candidate

    def _attachment_dir(self, space_name: str, page: dict) -> Path:
        title = self._sanitize(page.get("title", "untitled"))
        candidate = self.output_root / self._sanitize(space_name) / "attachments" / title
        if len(str(candidate.resolve())) > self.MAX_PATH_HINT:
            return self.output_root / self._sanitize(space_name) / "attachments" / "_flat"
        return candidate

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self) -> tuple[int, int, int]:
        """Execute the export. Returns ``(pages_written, pages_skipped, failures)``."""
        logger.info("Using %s", self.client.__class__.__name__)

        # 1) Identify space
        space = self.client.get_space(self.config.confluence.space_key)
        space_name = space.get("name") or self.config.confluence.space_key
        logger.info("Exporting space '%s' (%s)", space_name, self.config.confluence.space_key)

        # 2) List pages
        pages = self.client.get_all_pages(
            self.config.confluence.space_key,
            batch_size=self.config.export.batch_size,
        )
        total = len(pages)
        logger.info("Found %d pages", total)

        written = 0
        skipped = 0
        failed = 0

        for i, page in enumerate(pages, 1):
            title = page.get("title", "untitled")
            if self._progress:
                self._progress(title, i, total)

            try:
                if self._export_page(space_name, page):
                    written += 1
                else:
                    skipped += 1
                if self.config.export.include_attachments:
                    self._export_attachments(space_name, page)
            except ConfluenceError as e:
                failed += 1
                logger.error("  ! %s: %s", title, e)
            except Exception as e:
                failed += 1
                logger.exception("  ! Unexpected error on %s: %s", title, e)

        # Optional cleanup
        if self.config.export.cleanup_stale:
            self._cleanup_stale(pages)

        self._lockfile.save()
        logger.info(
            "Export done: %d written, %d skipped, %d failed (out of %d)",
            written, skipped, failed, total,
        )
        return written, skipped, failed

    # ------------------------------------------------------------------
    # Per-page
    # ------------------------------------------------------------------
    def _export_page(self, space_name: str, page: dict) -> bool:
        """Returns True if written, False if skipped (unchanged)."""
        page_id = page["id"]
        version = int(page.get("version", {}).get("number", 1))
        out_path = self._page_filepath(space_name, page)

        if self.config.export.skip_unchanged and self._lockfile.page_version(page_id) == version:
            return False

        safe_makedirs(out_path.parent)
        body_html = page.get("body", {}).get("storage", {}).get("value") or ""

        # Resolve attachments (filename -> local path) so clean_confluence_html can link them
        attachment_map: dict[str, str] = {}
        if self.config.export.include_attachments:
            attachment_map = self._prefetch_attachment_map(space_name, page)

        cleaned = clean_confluence_html(body_html, attachment_map)
        breadcrumbs = self._build_breadcrumbs(page) if self.config.export.page_breadcrumbs else ""
        title = page.get("title", "untitled")

        # For PDF format there is also Confluence's native renderer we can try
        if self.config.export.format == "pdf":
            pdf = self.client.get_page_pdf(page_id)
            if pdf:
                safe_write_bytes(out_path, pdf)
                self._lockfile.set_page(page_id, version, str(out_path))
                return True
            # Fall through to local rendering

        self._formatter.write(
            html_body=cleaned,
            output_path=str(out_path),
            page_title=title if self.config.export.include_document_title else "",
            breadcrumbs=breadcrumbs,
        )
        self._lockfile.set_page(page_id, version, str(out_path))
        return True

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------
    def _prefetch_attachment_map(self, space_name: str, page: dict) -> dict[str, str]:
        """Return ``{filename: local_absolute_path}`` downloading if needed."""
        mapping: dict[str, str] = {}
        attachments = self.client.get_attachments(page["id"])
        if not attachments:
            return mapping
        dest_dir = self._attachment_dir(space_name, page)
        safe_makedirs(dest_dir)

        # When we use the flat attachment dir, prefix filenames with page_id to
        # keep them unique across pages.
        flat = dest_dir.name == "_flat"

        for att in attachments:
            filename = att.get("title") or "attachment"
            filename = sanitize_filename(
                filename,
                encoding_map=self.config.export.filename_encoding,
                max_length=200,
            )
            out_name = f"{page['id']}_{filename}" if flat else filename
            out_path = dest_dir / out_name

            if out_path.exists():
                mapping[filename] = str(out_path.resolve())
                continue
            download_link = att.get("_links", {}).get("download")
            if not download_link:
                continue
            try:
                data = self.client.download_attachment(download_link)
                safe_write_bytes(out_path, data)
                mapping[filename] = str(out_path.resolve())
            except Exception as e:
                logger.warning("  Could not download attachment %s: %s", filename, e)
        return mapping

    def _export_attachments(self, space_name: str, page: dict) -> None:
        """Wrapper around the prefetch so a standalone call also works."""
        self._prefetch_attachment_map(space_name, page)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def _cleanup_stale(self, live_pages: list[dict]) -> None:
        """Remove pages in the lockfile that no longer exist in Confluence."""
        live_ids = {p["id"] for p in live_pages}
        stale = [pid for pid in list(self._lockfile._data.keys()) if pid not in live_ids]
        for pid in stale:
            entry = self._lockfile._data.get(pid, {})
            path = entry.get("path")
            if path and Path(path).exists():
                try:
                    Path(path).unlink()
                    logger.info("  - removed stale file: %s", path)
                except OSError:
                    pass
            self._lockfile.forget(pid)
