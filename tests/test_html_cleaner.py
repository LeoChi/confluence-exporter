from confluence_exporter.html_cleaner import clean_confluence_html


def test_strips_data_and_ac_attrs():
    src = '<p data-foo="bar" ac-macro="x">hello</p>'
    out = clean_confluence_html(src, {})
    assert "data-foo" not in out
    assert "ac-macro" not in out
    assert "hello" in out


def test_wraps_tables_in_tbody():
    src = "<table><tr><td>a</td><td>b</td></tr></table>"
    out = clean_confluence_html(src, {})
    assert "<tbody>" in out
    assert "conf-table" in out


def test_rewrites_ac_image_to_img():
    src = (
        '<ac:image ac:alt="demo">'
        '<ri:attachment ri:filename="pic.png"/></ac:image>'
    )
    out = clean_confluence_html(src, {"pic.png": "/tmp/pic.png"})
    assert "<img" in out
    assert "pic.png" in out


def test_code_macro_rendered_as_pre():
    src = (
        '<ac:structured-macro ac:name="code">'
        '<ac:plain-text-body>print(1)</ac:plain-text-body>'
        '</ac:structured-macro>'
    )
    out = clean_confluence_html(src, {})
    assert "<pre>" in out
    assert "print(1)" in out


def test_info_panel_becomes_styled_div():
    src = (
        '<ac:structured-macro ac:name="info">'
        '<ac:rich-text-body><p>hi</p></ac:rich-text-body>'
        '</ac:structured-macro>'
    )
    out = clean_confluence_html(src, {})
    assert 'class="conf-panel' in out
    assert "hi" in out


# ---------------------------------------------------------------------------
# Regression: empty macro bodies used to crash with bs4 IndexError.
# Reported on a real Nestle Confluence page ("VMA Database Encrypt Spike").
# ---------------------------------------------------------------------------


def test_empty_code_macro_does_not_crash():
    """A code macro with no body must NOT raise IndexError."""
    src = (
        '<ac:structured-macro ac:name="code">'
        '<ac:plain-text-body></ac:plain-text-body>'
        '</ac:structured-macro>'
    )
    out = clean_confluence_html(src, {})
    assert "<pre>" in out
    assert "<code>" in out


def test_code_macro_with_no_body_tag_at_all():
    src = '<ac:structured-macro ac:name="code"/>'
    out = clean_confluence_html(src, {})
    assert "<pre>" in out


def test_empty_info_panel_does_not_crash():
    src = (
        '<ac:structured-macro ac:name="info">'
        '<ac:rich-text-body></ac:rich-text-body>'
        '</ac:structured-macro>'
    )
    out = clean_confluence_html(src, {})
    assert 'class="conf-panel' in out


def test_panel_with_only_whitespace_body_does_not_crash():
    src = (
        '<ac:structured-macro ac:name="panel">'
        '<ac:rich-text-body>   \n\t  </ac:rich-text-body>'
        '</ac:structured-macro>'
    )
    out = clean_confluence_html(src, {})
    assert 'class="conf-panel' in out


def test_task_list_with_empty_body_does_not_crash():
    src = (
        '<ac:task-list>'
        '<ac:task><ac:task-status>incomplete</ac:task-status>'
        '<ac:task-body></ac:task-body></ac:task>'
        '</ac:task-list>'
    )
    out = clean_confluence_html(src, {})
    assert 'class="task-list"' in out
    assert "[ ]" in out
