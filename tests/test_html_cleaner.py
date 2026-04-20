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
