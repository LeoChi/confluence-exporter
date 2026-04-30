"""Tests for the incremental-update diff (PageDiff / compute_diff)."""

from __future__ import annotations

import threading
from pathlib import Path

from confluence_exporter.config import AppConfig
from confluence_exporter.exporter import (
    ExportResult,
    PageDiff,
    PageState,
    SpaceExporter,
)

# ---------------------------------------------------------------------------
# Fakes — we test the diff logic in isolation, no real HTTP / formatters.
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal stand-in for ConfluenceClient — only what the exporter touches."""

    def __init__(self, pages: list[dict]):
        self._pages = pages

    def get_all_pages(self, space_key: str, batch_size: int = 25) -> list[dict]:
        return self._pages

    def get_space(self, space_key: str) -> dict:
        return {"key": space_key, "name": "Test space"}

    # The rest aren't called by compute_diff


def _make_exporter(tmp_path: Path, pages: list[dict]) -> SpaceExporter:
    cfg = AppConfig()
    cfg.confluence.base_url = "https://example"
    cfg.confluence.space_key = "TST"
    cfg.confluence.auth_mode = "api_token"
    cfg.confluence.email = "x@y.z"
    cfg.confluence.api_token = "tok"
    cfg.export.format = "html"          # html avoids PDF-engine dependency
    cfg.export.output_path = str(tmp_path / "out")
    return SpaceExporter(cfg, _FakeClient(pages))


def _page(page_id: str, title: str, version: int) -> dict:
    return {
        "id": page_id,
        "title": title,
        "version": {"number": version},
        "ancestors": [],
        "body": {"storage": {"value": ""}},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_new_when_lockfile_empty(tmp_path):
    pages = [_page("1", "Foo", 1), _page("2", "Bar", 1)]
    exp = _make_exporter(tmp_path, pages)

    diff = exp.compute_diff()

    assert len(diff.new) == 2
    assert diff.updated == []
    assert diff.unchanged == []
    assert diff.deleted_ids == []
    assert diff.total_remote == 2


def test_unchanged_when_versions_match_and_file_exists(tmp_path):
    pages = [_page("1", "Foo", 3)]
    exp = _make_exporter(tmp_path, pages)
    # Pretend we already exported v3 of page 1 and the file is on disk
    fake_file = tmp_path / "Foo.html"
    fake_file.write_text("<p>old</p>")
    exp._lockfile.set_page("1", 3, str(fake_file))

    diff = exp.compute_diff()

    assert diff.new == []
    assert diff.updated == []
    assert len(diff.unchanged) == 1
    assert diff.deleted_ids == []


def test_updated_when_remote_version_is_newer(tmp_path):
    pages = [_page("1", "Foo", 5)]   # remote is at v5
    exp = _make_exporter(tmp_path, pages)
    fake_file = tmp_path / "Foo.html"
    fake_file.write_text("<p>old</p>")
    exp._lockfile.set_page("1", 3, str(fake_file))   # we have v3

    diff = exp.compute_diff()

    assert len(diff.updated) == 1
    assert diff.updated[0]["id"] == "1"
    assert diff.unchanged == []
    assert diff.new == []


def test_updated_when_local_file_was_deleted(tmp_path):
    """Same version recorded, but the file is gone on disk → re-download."""
    pages = [_page("1", "Foo", 3)]
    exp = _make_exporter(tmp_path, pages)
    missing = tmp_path / "this-file-was-deleted.html"
    exp._lockfile.set_page("1", 3, str(missing))

    diff = exp.compute_diff()

    assert len(diff.updated) == 1, "missing file should force re-download"
    assert diff.unchanged == []


def test_deleted_upstream(tmp_path):
    """Page is in the lockfile but not in Confluence anymore."""
    pages: list[dict] = []                     # nothing in Confluence
    exp = _make_exporter(tmp_path, pages)
    fake = tmp_path / "Old.html"
    fake.write_text("dead")
    exp._lockfile.set_page("99", 1, str(fake))

    diff = exp.compute_diff()

    assert diff.deleted_ids == ["99"]
    assert diff.new == []
    assert diff.updated == []


def test_mixed_diff_buckets(tmp_path):
    pages = [
        _page("1", "New page", 1),       # NEW
        _page("2", "Updated", 7),        # UPDATED (remote 7 > local 3)
        _page("3", "Stable", 4),         # UNCHANGED
    ]
    exp = _make_exporter(tmp_path, pages)
    f2 = tmp_path / "Updated.html"
    f2.write_text("v3")
    f3 = tmp_path / "Stable.html"
    f3.write_text("v4")
    exp._lockfile.set_page("2", 3, str(f2))
    exp._lockfile.set_page("3", 4, str(f3))
    exp._lockfile.set_page("4", 1, str(tmp_path / "Gone.html"))   # DELETED upstream

    diff = exp.compute_diff()

    assert [p["id"] for p in diff.new] == ["1"]
    assert [p["id"] for p in diff.updated] == ["2"]
    assert [p["id"] for p in diff.unchanged] == ["3"]
    assert diff.deleted_ids == ["4"]
    assert diff.total_remote == 3


def test_to_download_is_new_plus_updated(tmp_path):
    pages = [_page("1", "A", 1), _page("2", "B", 5)]
    exp = _make_exporter(tmp_path, pages)
    f2 = tmp_path / "B.html"
    f2.write_text("old")
    exp._lockfile.set_page("2", 3, str(f2))

    diff = exp.compute_diff()

    ids = [p["id"] for p in diff.to_download]
    assert sorted(ids) == ["1", "2"]


def test_export_result_is_tuple_unpackable():
    r = ExportResult(new_count=2, updated_count=1, unchanged_count=10, failed_count=0)
    written, skipped, failed = r
    assert written == 3
    assert skipped == 10
    assert failed == 0


def test_classify_page_returns_states(tmp_path):
    pages = [_page("1", "X", 1)]
    exp = _make_exporter(tmp_path, pages)
    assert exp._classify_page(pages[0]) == PageState.NEW

    fake = tmp_path / "X.html"
    fake.write_text("ok")
    exp._lockfile.set_page("1", 1, str(fake))
    assert exp._classify_page(pages[0]) == PageState.UNCHANGED

    pages[0]["version"]["number"] = 2
    assert exp._classify_page(pages[0]) == PageState.UPDATED


def test_pagediff_summary_keys():
    d = PageDiff(new=[{"id": "1"}], updated=[], unchanged=[{"id": "2"}], deleted_ids=["3"])
    s = d.summary()
    assert s == {"new": 1, "updated": 0, "unchanged": 1, "deleted": 1}


def test_cancel_event_stops_run_loop_between_pages(tmp_path):
    """Setting cancel_event before a page is processed should bail out cleanly."""
    pages = [
        _page("1", "First", 1),
        _page("2", "Second", 1),
        _page("3", "Third", 1),
    ]
    exp = _make_exporter(tmp_path, pages)

    cancel = threading.Event()

    # Inject the cancel event after construction (simulating what the GUI does)
    exp._cancel_event = cancel

    # Cancel after the first page is "exported" by hooking the progress callback.
    call_count = {"n": 0}

    def cb(_title, _i, _total) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:
            # Trip cancel right before the loop checks again
            cancel.set()

    exp._progress = cb

    result = exp.run()

    # Should have stopped before processing all 3 pages
    assert call_count["n"] <= 2
    # Whatever was processed should be reflected; cancel did NOT crash
    assert isinstance(result, ExportResult)


def test_cancel_event_set_before_run_processes_zero_pages(tmp_path):
    pages = [_page("1", "A", 1), _page("2", "B", 1)]
    exp = _make_exporter(tmp_path, pages)

    cancel = threading.Event()
    cancel.set()  # cancelled before we even started
    exp._cancel_event = cancel

    result = exp.run()
    assert result.new_count == 0
    assert result.updated_count == 0
