from confluence_exporter.filename import sanitize_filename, short_section_name


def test_sanitize_strips_unsafe_chars():
    s = sanitize_filename('bad/name?with*chars:"and<>more"|')
    for bad in '/?*:"<>|':
        assert bad not in s


def test_sanitize_respects_max_length():
    name = "a" * 400
    result = sanitize_filename(name, max_length=50)
    assert len(result) <= 50
    # Preserves the leading chars and appends a short hash for uniqueness
    assert result.startswith("a" * 30)


def test_sanitize_lowercase():
    assert sanitize_filename("FoO BaR") == "FoO BaR"
    assert sanitize_filename("FoO BaR", lowercase=True) == "foo bar"


def test_sanitize_empty_returns_placeholder():
    assert sanitize_filename("") == "_"
    assert sanitize_filename("   ") == "_"


def test_short_section_name_respects_length():
    assert len(short_section_name("x" * 300, max_length=100)) <= 100
