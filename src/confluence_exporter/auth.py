"""Authentication strategies.

Three concrete providers implement the :class:`AuthProvider` contract
(Strategy pattern). Pick one with :func:`build_auth` from an
:class:`~confluence_exporter.config.ConfluenceConfig`.

The cookie provider is **generic**: it doesn't care what the session cookie
is called (``cloud.session.token``, ``tenant.session.token``, ``JSESSIONID``,
etc.) — it just forwards every cookie the browser would send. Users can paste
a full ``Cookie:`` header from DevTools and we parse it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import requests
from requests.auth import AuthBase, HTTPBasicAuth

from confluence_exporter.config import ConfluenceConfig

# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class AuthProvider(ABC):
    """Applies the correct credentials to a ``requests.Session`` or Request."""

    name: str = "abstract"

    @abstractmethod
    def apply(self, session: requests.Session) -> None:  # pragma: no cover - abstract
        ...

    @property
    def description(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# Concrete strategies
# ---------------------------------------------------------------------------


class ApiTokenAuth(AuthProvider):
    name = "api_token"

    def __init__(self, email: str, api_token: str):
        self._auth: AuthBase = HTTPBasicAuth(email, api_token)
        self._email = email

    def apply(self, session: requests.Session) -> None:
        session.auth = self._auth
        session.headers.setdefault("Accept", "application/json")

    @property
    def description(self) -> str:
        return f"Basic auth (email={self._email})"


class PersonalAccessTokenAuth(AuthProvider):
    name = "pat"

    def __init__(self, token: str):
        self._token = token

    def apply(self, session: requests.Session) -> None:
        session.headers["Authorization"] = f"Bearer {self._token}"
        session.headers.setdefault("Accept", "application/json")

    @property
    def description(self) -> str:
        tail = self._token[-4:] if len(self._token) > 4 else "****"
        return f"Bearer PAT (…{tail})"


class BrowserCookieAuth(AuthProvider):
    """Sends the exact set of cookies your browser would send.

    Accepts any mix of Atlassian session cookies — the script neither hard-codes
    a cookie name nor tries to read them from your browser profile (which often
    fails with locked SQLite files and DPAPI issues on Windows).
    """

    name = "browser_cookie"

    def __init__(self, cookies: dict[str, str]):
        if not cookies:
            raise ValueError("BrowserCookieAuth requires at least one cookie")
        self._cookies = dict(cookies)

    def apply(self, session: requests.Session) -> None:
        for k, v in self._cookies.items():
            session.cookies.set(k, v)
        # Atlassian returns HTML for some endpoints when Accept is missing
        session.headers.setdefault("Accept", "application/json")
        session.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (compatible; confluence-exporter/0.1)",
        )

    @property
    def description(self) -> str:
        return f"Cookie auth ({len(self._cookies)} cookie(s))"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_auth(conf: ConfluenceConfig) -> AuthProvider:
    """Factory returning the correct strategy for the configured mode."""
    mode = (conf.auth_mode or "api_token").lower()
    if mode == "api_token":
        return ApiTokenAuth(conf.email, conf.api_token)
    if mode == "pat":
        return PersonalAccessTokenAuth(conf.personal_access_token)
    if mode == "browser_cookie":
        return BrowserCookieAuth(conf.cookies)
    raise ValueError(f"Unknown auth_mode: {mode!r}")


# ---------------------------------------------------------------------------
# Cookie parsing (generic)
# ---------------------------------------------------------------------------


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Parse anything the user might paste into a ``{name: value}`` dict.

    Accepts:

    * A full ``Cookie:`` header copied from DevTools
      (``Cookie: a=1; b=2; c=3``).
    * A semicolon-separated string without the ``Cookie:`` prefix.
    * One-per-line ``name=value`` entries (blank lines and # comments skipped).
    * JSON ``{"name": "value", ...}`` pasted as-is.

    Leading/trailing quotes around values are stripped; whitespace is trimmed.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}

    # JSON object?
    if raw.startswith("{"):
        try:
            import json

            data = json.loads(raw)
            if isinstance(data, dict):
                return {str(k).strip(): str(v).strip().strip('"').strip("'")
                        for k, v in data.items() if k}
        except Exception:
            pass  # fall through to textual parse

    # Strip leading "Cookie:" prefix if present
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()

    result: dict[str, str] = {}

    # Heuristic: is this semicolon-delimited or newline-delimited?
    # Use lines if the raw text contains newlines AND not primarily ';'.
    if "\n" in raw and raw.count(";") <= raw.count("\n"):
        chunks = raw.splitlines()
    else:
        chunks = raw.split(";")

    for chunk in chunks:
        chunk = chunk.strip().lstrip("#").strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name:
            result[name] = value
    return result


def merge_cookies(*groups: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for g in groups:
        if g:
            out.update(g)
    return out


# ---------------------------------------------------------------------------
# Discovery hints (for interactive setup)
# ---------------------------------------------------------------------------


#: A non-exhaustive list of session-cookie names Atlassian uses across tenants.
#: Printed as hints; we don't filter on them.
KNOWN_ATLASSIAN_SESSION_COOKIES: tuple[str, ...] = (
    "cloud.session.token",
    "tenant.session.token",
    "cloud.session.token.skip.touch",
    "atlassian.xsrf.token",
    "JSESSIONID",
)


def find_likely_session_cookies(cookies: dict[str, str]) -> list[str]:
    """Return the names present in ``cookies`` that look like session tokens."""
    return [name for name in cookies if name in KNOWN_ATLASSIAN_SESSION_COOKIES
            or "session" in name.lower() or "token" in name.lower()]
