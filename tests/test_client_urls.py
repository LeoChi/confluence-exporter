"""Tests for ConfluenceClient URL construction.

These don't make any real HTTP calls — they exercise the pure URL-building
logic that turns ``_links.download`` paths into absolute URLs.
"""

from __future__ import annotations

from confluence_exporter.auth import ApiTokenAuth
from confluence_exporter.client import ConfluenceClient


def _client(base: str = "https://example.atlassian.net") -> ConfluenceClient:
    return ConfluenceClient(
        base_url=base,
        auth=ApiTokenAuth(email="x@y.z", api_token="tok"),
        request_delay_seconds=0,
    )


# ---------------------------------------------------------------------------
# _attachment_url — the regression case the user hit on nestle.atlassian.net
# ---------------------------------------------------------------------------


def test_relative_download_path_gets_wiki_prefix():
    """The Cloud regression: API returns ``/download/...`` and we MUST prepend ``/wiki``."""
    c = _client()
    url = c._attachment_url("/download/attachments/87524725/foo.png?version=1")
    assert url == (
        "https://example.atlassian.net/wiki/download/attachments/87524725/foo.png?version=1"
    )


def test_relative_path_already_contains_wiki_is_not_double_prefixed():
    c = _client()
    url = c._attachment_url("/wiki/download/attachments/1/x.png")
    assert url == "https://example.atlassian.net/wiki/download/attachments/1/x.png"


def test_relative_path_without_leading_slash_still_works():
    c = _client()
    url = c._attachment_url("download/attachments/1/x.png")
    assert url == "https://example.atlassian.net/wiki/download/attachments/1/x.png"


def test_absolute_url_is_passed_through_unchanged():
    c = _client()
    full = "https://example.atlassian.net/wiki/download/attachments/1/x.png?v=2"
    assert c._attachment_url(full) == full


def test_absolute_url_on_different_host_is_passed_through():
    """If Confluence ever returns a CDN URL we don't want to mangle it."""
    c = _client()
    cdn = "https://cdn.atl.example.com/foo/bar.png"
    assert c._attachment_url(cdn) == cdn


def test_base_url_with_trailing_slash_is_normalised():
    c = _client("https://example.atlassian.net/")
    url = c._attachment_url("/download/attachments/1/x.png")
    assert url == "https://example.atlassian.net/wiki/download/attachments/1/x.png"


def test_api_root_still_includes_wiki_context():
    """Sanity check: existing API calls still go to /wiki/rest/api/ ."""
    c = _client()
    assert c.api_root == "https://example.atlassian.net/wiki/rest/api/"
