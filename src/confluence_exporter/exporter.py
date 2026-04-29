"""Download a Confluence space to disk (pages + attachments)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

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


# ---------------------------------------------------------------------------
# Incremental-update model
# ---------------------------------------------------------------------------


class PageState(str, Enum):
    """Status of a single page when comparing Confluence against local state."""

    NEW = "new"             # in Confluence, not in lockfile
    UPDATED = "updated"     # in lockfile but Confluence version is newer, OR file missing
    UNCHANGED = "unchanged" # already up to date and file exists
    DELETED = "deleted"     # in lockfile but no longer in Confluence


@dataclass
class PageDiff:
    """Result of :meth:`SpaceExporter.compute_diff`.

    Buckets every Confluence page into one of the four :class:`PageState`
    categories so callers can preview what a re-run would do.
    """

    new: list[dict] = field(default_factory=list)
    updated: list[dict] = field(default_factory=list)
    unchanged: list[dict] = field(default_factory=list)
    deleted_ids: list[str] = field(default_factory=list)

    @property
    def to_download(self) -> list[dict]:
        """Pages that need work — the union of NEW and UPDATED."""
        return self.new + self.updated

    @property
    def total_remote(self) -> int:
        return len(self.new) + len(self.updated) + len(self.unchanged)

    def summary(self) -> dict[str, int]:
        return {
            "new":       len(self.new),
            "updated":   len(self.updated),
            "unchanged": len(self.unchanged),
            "deleted":   len(self.deleted_ids),
        }


@dataclass
class ExportResult:
    """Richer return type of :meth:`SpaceExporter.run`.

    Backward-compatible: still iterable as ``(written, skipped, failed)`` so
    legacy callers continue to work.
    """

    new_count: int = 0
    updated_count: int = 0
    unchanged_count: int = 0
    failed_count: int = 0
    deleted_upstream: int = 0

    @property
    def written(self) -> int:
        return self.new_count + self.updated_count

    # Tuple-unpacking: ``written, skipped, failed = exporter.run()``
    def __iter__(self):
        yield self.written
        yield self.unchanged_count
        yield self.failed_count


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
    # Diff (incremental update detection)
    # ------------------------------------------------------------------
    def _classify_page(self, page: dict) -> PageState:
        """Decide what state a single Confluence page is in vs. local state."""
        page_id = page["id"]
        remote_version = int(page.get("version", {}).get("number", 1))
        local_version = self._lockfile.page_version(page_id)

        if local_version == 0:
            return PageState.NEW
        if local_version < remote_version:
            return PageState.UPDATED

        # Same version recorded — but check the file is actually still on disk.
        # If the user deleted it locally we want to re-download it.
        recorded_path = self._lockfile._data.get(page_id, {}).get("path")
        if recorded_path and not Path(recorded_path).exists():
            return PageState.UPDATED  # treat missing-on-disk as needing re-download

        return PageState.UNCHANGED

    def compute_diff(self, pages: list[dict] | None = None) -> PageDiff:
        """Categorise the live space against the local lockfile + disk state.

        This does **no downloading**. Use it for "what would change?" previews
        and for the ``status`` CLI subcommand.

        Parameters
        ----------
        pages
            Optional list of pages already fetched (avoids re-listing). If
            omitted, the method calls
            :meth:`ConfluenceClient.get_all_pages` itself.
        """
        if pages is None:
            pages = self.client.get_all_pages(
                self.config.confluence.space_key,
                batch_size=self.config.export.batch_size,
            )

        diff = PageDiff()
        seen_ids: set[str] = set()

        for page in pages:
            seen_ids.add(page["id"])
            state = self._classify_page(page)
            if state == PageState.NEW:
                diff.new.append(page)
            elif state == PageState.UPDATED:
                diff.updated.append(page)
            else:
                diff.unchanged.append(page)

        diff.deleted_ids = [pid for pid in self._lockfile._data if pid not in seen_ids]
        return diff

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self) -> ExportResult:
        """Execute the export.

        Returns an :class:`ExportResult` with separate counts for *new* and
        *updated* pages (still tuple-unpackable as
        ``(written, skipped, failed)`` for backward compatibility).
        """
        logger.info("Using %s", self.client.__class__.__name__)

        # 1) Identify space
        space = self.client.get_space(self.config.confluence.space_key)
        space_name = space.get("name") or self.config.confluence.space_key
        logger.info("Exporting space '%s' (%s)", space_name, self.config.confluence.space_key)

        # 2) List pages and compute the diff up front
        pages = self.client.get_all_pages(
            self.config.confluence.space_key,
            batch_size=self.config.export.batch_size,
        )
        logger.info("Found %d pages", len(pages))

        diff = self.compute_diff(pages)
        s = diff.summary()
        logger.info(
            "Diff: %d new, %d updated, %d unchanged, %d deleted upstream",
            s["new"], s["updated"], s["unchanged"], s["deleted"],
        )

        result = ExportResult(
            unchanged_count=s["unchanged"],
            deleted_upstream=s["deleted"],
        )

        # When skip_unchanged is off, fall back to processing every page.
        # Otherwise only download new + updated pages.
        if self.config.export.skip_unchanged:
            to_process = diff.to_download
        else:
            to_process = diff.new + diff.updated + diff.unchanged
            # We're going to overwrite everything; "unchanged" is no longer free.
            result.unchanged_count = 0

        total_work = len(to_process)
        logger.info("Will process %d page(s)", total_work)

        new_ids = {p["id"] for p in diff.new}

        for i, page in enumerate(to_process, 1):
            title = page.get("title", "untitled")
            if self._progress:
                self._progress(title, i, total_work)

            try:
                wrote = self._export_page(space_name, page)
                if wrote:
                    if page["id"] in new_ids:
                        result.new_count += 1
                    else:
                        result.updated_count += 1
                if self.config.export.include_attachments:
                    self._export_attachments(space_name, page)
            except ConfluenceError as e:
                result.failed_count += 1
                logger.error("  ! %s: %s", title, e)
            except Exception as e:
                result.failed_count += 1
                logger.exception("  ! Unexpected error on %s: %s", title, e)

        # Optional cleanup of pages that disappeared upstream
        if self.config.export.cleanup_stale and diff.deleted_ids:
            self._cleanup_stale_ids(diff.deleted_ids)

        self._lockfile.save()
        logger.info(
            "Export done: %d new, %d updated, %d unchanged, %d failed (out of %d)",
            result.new_count, result.updated_count, result.unchanged_count,
            result.failed_count, len(pages),
        )
        return result

    # ------------------------------------------------------------------
    # Per-page
    # ------------------------------------------------------------------
    def _export_page(self, space_name: str, page: dict) -> bool:
        """Returns True if written, False if skipped (unchanged).

        The classification is delegated to :meth:`_classify_page`, which also
        treats a missing-on-disk file as "needs re-download".
        """
        page_id = page["id"]
        version = int(page.get("version", {}).get("number", 1))
        out_path = self._page_filepath(space_name, page)

        if (
            self.config.export.skip_unchanged
            and self._classify_page(page) == PageState.UNCHANGED
        ):
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
        """Remove pages in the lockfile that no longer exist in Confluence.

        Kept for backward compatibility — the new run() flow calls
        :meth:`_cleanup_stale_ids` directly using the diff result.
        """
        live_ids = {p["id"] for p in live_pages}
        stale = [pid for pid in list(self._lockfile._data.keys()) if pid not in live_ids]
        self._cleanup_stale_ids(stale)

    def _cleanup_stale_ids(self, stale_ids: list[str]) -> None:
        """Delete files + lockfile entries for the given page IDs."""
        import contextlib
        for pid in stale_ids:
            entry = self._lockfile._data.get(pid, {})
            path = entry.get("path")
            if path and Path(path).exists():
                with contextlib.suppress(OSError):
                    Path(path).unlink()
                    logger.info("  - removed stale file: %s", path)
            self._lockfile.forget(pid)
