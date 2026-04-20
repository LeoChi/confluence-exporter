import json

from confluence_exporter.config import AppConfig, load_config, save_config


def test_defaults():
    cfg = AppConfig()
    assert cfg.confluence.auth_mode == "api_token"
    assert cfg.export.format == "pdf"
    assert cfg.convert.engine == "auto"
    assert cfg.merge.mode == "per_section"


def test_validate_requires_base_url_and_space():
    cfg = AppConfig()
    errs = cfg.validate()
    assert any("base_url" in e for e in errs)
    assert any("space_key" in e for e in errs)


def test_validate_api_token_mode():
    cfg = AppConfig()
    cfg.confluence.base_url = "https://x.atlassian.net"
    cfg.confluence.space_key = "ABC"
    errs = cfg.validate()
    # api_token mode with missing email+token must complain
    assert any("email" in e for e in errs)
    assert any("api_token" in e for e in errs)


def test_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    cfg = AppConfig()
    cfg.confluence.base_url = "https://foo.atlassian.net"
    cfg.confluence.space_key = "FOO"
    save_config(cfg, path)

    # simulate comment keys being present
    data = json.loads(path.read_text())
    data["_comment"] = "ignore me"
    path.write_text(json.dumps(data))

    loaded = load_config(path)
    assert loaded.confluence.base_url == "https://foo.atlassian.net"
    assert loaded.confluence.space_key == "FOO"


def test_from_dict_tolerates_unknown_keys():
    data = {
        "confluence": {"base_url": "x", "unknown_key": 1},
        "export": {"format": "md"},
        "extra_top_level": 99,
    }
    cfg = AppConfig.from_dict(data)
    assert cfg.confluence.base_url == "x"
    assert cfg.export.format == "md"
