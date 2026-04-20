"""Confluence REST client (Facade).

Encapsulates auth + retries + pagination + rate limiting around the handful of
endpoints we use. Everything else in the package talks to Confluence through
this class.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin

import requests

from confluence_exporter.auth import AuthProvider, build_auth
from confluence_exporter.config import ConfluenceConfig
from confluence_exporter.logging_utils import get_logger

logger = get_logger()


class ConfluenceError(RuntimeError):
    """Raised for HTTP-level failures we can't recover from."""


class ConfluenceClient:
    """Thin REST client around Confluence Cloud / Server ``/wiki/rest/api``."""

    def __init__(
        self,
        base_url: str,
        auth: AuthProvider,
        request_delay_seconds: float = 0.25,
        timeout: float = 30.0,
    ):
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.api_root = self.base_url + "/wiki/rest/api/"
        self.request_delay = max(0.0, request_delay_seconds)
        self.timeout = timeout

        self._session = requests.Session()
        auth.apply(self._session)
        self._auth_desc = auth.description

    # ---- factory --------------------------------------------------------
    @classmethod
    def from_config(cls, conf: ConfluenceConfig, *, request_delay_seconds: float = 0.25) -> ConfluenceClient:
        return cls(
            base_url=conf.base_url,
            auth=build_auth(conf),
            request_delay_seconds=request_delay_seconds,
        )

    # ---- low-level HTTP -------------------------------------------------
    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return urljoin(self.api_root, path.lstrip("/"))

    def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
    ) -> requests.Response:
        headers: dict[str, str] = {}
        if accept:
            headers["Accept"] = accept
        if self.request_delay:
            time.sleep(self.request_delay)
        try:
            resp = self._session.get(
                self._url(path), params=params, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as e:
            raise ConfluenceError(f"GET {path}: network error: {e}") from e
        if resp.status_code == 401:
            raise ConfluenceError(
                f"HTTP 401 Unauthorized — check credentials ({self._auth_desc}). "
                "If you used an API token, your tenant admin may have locked it; "
                "try the browser-cookie auth instead."
            )
        if resp.status_code == 403:
            raise ConfluenceError(
                f"HTTP 403 Forbidden on {path}. Your account lacks permission "
                "for that resource, or the endpoint is disabled on this tenant."
            )
        if resp.status_code >= 400:
            raise ConfluenceError(
                f"HTTP {resp.status_code} on {path}: {resp.text[:200]}"
            )
        return resp

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._get(path, params=params, accept="application/json").json()

    # ---- typed endpoints ------------------------------------------------
    def test_connection(self) -> dict[str, Any]:
        """GET /user/current — raises on any error; returns the user payload."""
        return self._get_json("user/current")

    def list_spaces(self, limit: int = 500) -> list[dict[str, Any]]:
        """All spaces visible to the authenticated user."""
        spaces: list[dict[str, Any]] = []
        start = 0
        step = 50
        while len(spaces) < limit:
            data = self._get_json("space", params={"limit": step, "start": start})
            chunk = data.get("results", [])
            if not chunk:
                break
            spaces.extend(chunk)
            if data.get("_links", {}).get("next"):
                start += step
            else:
                break
        return spaces[:limit]

    def get_space(self, space_key: str) -> dict[str, Any]:
        return self._get_json(f"space/{space_key}")

    def get_all_pages(
        self, space_key: str, *, batch_size: int = 25
    ) -> list[dict[str, Any]]:
        """All pages in a space, with ancestor + body.storage fields inlined."""
        pages: list[dict[str, Any]] = []
        start = 0
        while True:
            data = self._get_json(
                "content",
                params={
                    "spaceKey": space_key,
                    "type": "page",
                    "limit": batch_size,
                    "start": start,
                    "expand": "ancestors,version,body.storage,children.attachment",
                },
            )
            chunk = data.get("results", [])
            if not chunk:
                break
            pages.extend(chunk)
            if data.get("_links", {}).get("next"):
                start += batch_size
            else:
                break
        return pages

    def get_attachments(
        self, page_id: str, *, batch_size: int = 25
    ) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        start = 0
        while True:
            data = self._get_json(
                f"content/{page_id}/child/attachment",
                params={"limit": batch_size, "start": start},
            )
            chunk = data.get("results", [])
            if not chunk:
                break
            attachments.extend(chunk)
            if data.get("_links", {}).get("next"):
                start += batch_size
            else:
                break
        return attachments

    def download_attachment(self, download_path: str) -> bytes:
        """Download an attachment by its ``_links.download`` path."""
        url = urljoin(self.base_url + "/", download_path.lstrip("/"))
        if self.request_delay:
            time.sleep(self.request_delay)
        resp = self._session.get(url, timeout=self.timeout)
        if resp.status_code >= 400:
            raise ConfluenceError(f"Attachment download failed ({resp.status_code}): {url}")
        return resp.content

    def get_page_pdf(self, page_id: str) -> bytes | None:
        """Try Confluence's native PDF export. Returns bytes or None if blocked.

        We try two endpoints in order because tenants disable one or the other:
        ``/content/{id}/export/pdf`` and ``/spaces/flyingpdf/pdfpageexport.action``.
        """
        # 1) REST export
        try:
            resp = self._get(
                f"content/{page_id}/export/pdf", accept="application/pdf"
            )
            if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                return resp.content
        except ConfluenceError:
            pass

        # 2) flyingpdf action
        url = (
            self.base_url
            + f"/wiki/spaces/flyingpdf/pdfpageexport.action?pageId={page_id}"
        )
        if self.request_delay:
            time.sleep(self.request_delay)
        try:
            resp = self._session.get(url, timeout=self.timeout)
            if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                return resp.content
        except requests.RequestException:
            pass
        return None
