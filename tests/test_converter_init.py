"""Tests for OutputConverter construction.

Specifically a regression guard for the 0.1.x bug where the attachment
indexes were never initialised, causing the very first call to ``.run()``
to raise:

    AttributeError: 'OutputConverter' object has no attribute
    '_attachments_by_title'
"""

from __future__ import annotations

import threading
from pathlib import Path

from confluence_exporter.converter import OutputConverter


def _make(tmp_path: Path) -> OutputConverter:
    src = tmp_path / "exported"
    src.mkdir()
    return OutputConverter(output_root=src, target_format="pdf")


def test_attachments_indexes_are_initialised(tmp_path):
    """Constructor must populate both attachment lookup dicts."""
    c = _make(tmp_path)
    assert isinstance(c._attachments_by_title, dict)
    assert isinstance(c._attachments_by_pageid, dict)


def test_run_does_not_crash_on_empty_source(tmp_path):
    """An empty source folder should produce 0/0, not AttributeError."""
    c = _make(tmp_path)
    ok_n, fail_n = c.run()
    assert ok_n == 0
    assert fail_n == 0


def test_cancel_event_attribute_present(tmp_path):
    """The cancel_event added in feat/cancellation-and-ux must still work."""
    c = _make(tmp_path)
    assert c._cancel_event is None
    assert c._is_cancelled() is False

    ev = threading.Event()
    c2 = OutputConverter(output_root=tmp_path / "exported", target_format="pdf",
                         cancel_event=ev)
    assert c2._is_cancelled() is False
    ev.set()
    assert c2._is_cancelled() is True


def test_attachments_dirs_scanned_when_present(tmp_path):
    """If the source has the standard layout, the title-keyed index gets populated."""
    src = tmp_path / "exported"
    space = src / "MySpace" / "attachments"
    page_attach = space / "MyPage"
    page_attach.mkdir(parents=True)
    (page_attach / "diagram.png").write_bytes(b"x")

    c = OutputConverter(output_root=src, target_format="pdf")
    assert "MyPage" in c._attachments_by_title
    assert c._attachments_by_title["MyPage"] == page_attach
