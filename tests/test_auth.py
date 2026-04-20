import pytest

from confluence_exporter.auth import (
    ApiTokenAuth,
    BrowserCookieAuth,
    PersonalAccessTokenAuth,
    build_auth,
    find_likely_session_cookies,
    parse_cookie_header,
)
from confluence_exporter.config import ConfluenceConfig


def test_parse_full_cookie_header():
    raw = "Cookie: a=1; b=two; cloud.session.token=abc.def"
    out = parse_cookie_header(raw)
    assert out == {"a": "1", "b": "two", "cloud.session.token": "abc.def"}


def test_parse_no_prefix_semicolons():
    assert parse_cookie_header("a=1; b=2") == {"a": "1", "b": "2"}


def test_parse_lines():
    raw = "a=1\nb=2\n# comment\n  c = 3  "
    assert parse_cookie_header(raw) == {"a": "1", "b": "2", "c": "3"}


def test_parse_strips_quotes():
    assert parse_cookie_header('foo="bar"') == {"foo": "bar"}


def test_parse_json_dict():
    assert parse_cookie_header('{"x": "y", "z": "w"}') == {"x": "y", "z": "w"}


def test_parse_empty():
    assert parse_cookie_header("") == {}
    assert parse_cookie_header("   ") == {}


def test_find_likely_session_cookies_prefers_known_names():
    hits = find_likely_session_cookies({
        "cloud.session.token": "a",
        "tenant.session.token": "b",
        "ad_pref": "c",
    })
    assert "cloud.session.token" in hits
    assert "tenant.session.token" in hits
    assert "ad_pref" not in hits


def test_find_likely_session_cookies_heuristic_match():
    hits = find_likely_session_cookies({"my.session.weird": "a", "xsrf_token": "b"})
    assert "my.session.weird" in hits
    assert "xsrf_token" in hits


def test_build_auth_api_token():
    cfg = ConfluenceConfig(auth_mode="api_token", email="e@x.com", api_token="tok")
    assert isinstance(build_auth(cfg), ApiTokenAuth)


def test_build_auth_pat():
    cfg = ConfluenceConfig(auth_mode="pat", personal_access_token="pat")
    assert isinstance(build_auth(cfg), PersonalAccessTokenAuth)


def test_build_auth_cookie():
    cfg = ConfluenceConfig(auth_mode="browser_cookie", cookies={"k": "v"})
    assert isinstance(build_auth(cfg), BrowserCookieAuth)


def test_build_auth_unknown():
    cfg = ConfluenceConfig(auth_mode="nope")
    with pytest.raises(ValueError):
        build_auth(cfg)
