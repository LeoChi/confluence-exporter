import os

from confluence_exporter.paths import (
    is_valid_pdf,
    long_path,
    move_into_place,
    safe_write_bytes,
)


def test_long_path_is_idempotent():
    assert long_path(long_path("/tmp/foo")) == long_path("/tmp/foo")


def test_long_path_noop_on_posix(tmp_path):
    # When not on Windows, long_path is a no-op
    if os.name != "nt":
        assert long_path(str(tmp_path)) == str(tmp_path)


def test_is_valid_pdf_rejects_missing(tmp_path):
    assert is_valid_pdf(str(tmp_path / "nope.pdf")) is False


def test_is_valid_pdf_rejects_empty(tmp_path):
    p = tmp_path / "empty.pdf"
    p.write_bytes(b"")
    assert is_valid_pdf(str(p)) is False


def test_is_valid_pdf_accepts_pdf_header(tmp_path):
    p = tmp_path / "good.pdf"
    # pad the content above the 1 KB threshold
    p.write_bytes(b"%PDF-1.4\n" + b"0" * 2000)
    assert is_valid_pdf(str(p)) is True


def test_safe_write_and_move(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "sub" / "b.bin"
    assert safe_write_bytes(a, b"hello")
    assert a.read_bytes() == b"hello"
    assert move_into_place(a, b)
    assert b.read_bytes() == b"hello"
    assert not a.exists()
