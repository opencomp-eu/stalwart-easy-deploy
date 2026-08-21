"""Tests for scripts/apply.py."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.apply import (
    COMPOSE_PROJECT_NAME,
    derive_compose_files,
    ensure_data_dirs,
    load_or_create_secrets,
    public_url,
    render_caddyfile,
    render_template,
    site_blocks,
    stalwart_https_upstream,
    validate_config,
    write_compose_env,
)


def _base_config(**overrides) -> dict:
    config = {
        "stalwart": {
            "hostname": "mail.test.example",
            "domain": "test.example",
            "image": "docker.io/stalwartlabs/stalwart",
            "tag": "v0.16",
            "data_dir": "/var/lib/stalwart",
        },
        "bulwark": {
            "enabled": True,
            "domain": "webmail.test.example",
            "image": "ghcr.io/bulwarkmail/webmail",
            "tag": "1.7.5",
            "data_dir": "/var/lib/bulwark",
            "app_name": "Webmail",
        },
        "ports": {
            "smtp": 25,
            "submission": 587,
            "submissions": 465,
            "imaps": 993,
            "sieve": 4190,
        },
        "proxy": {"type": "caddy", "mode": "standalone", "integrate": {"network": "easydeploy-net"}},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and key in config and isinstance(config[key], dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


def test_validate_config_rejects_placeholder_hostname():
    with pytest.raises(ValueError, match="stalwart.hostname"):
        validate_config(
            _base_config(stalwart={"hostname": "mail.example.com", "domain": "real.example", "data_dir": "/x"})
        )


def test_validate_config_rejects_placeholder_domain():
    with pytest.raises(ValueError, match="stalwart.domain"):
        validate_config(
            _base_config(stalwart={"hostname": "mail.real.example", "domain": "example.com", "data_dir": "/x"})
        )


def test_validate_config_requires_webmail_domain():
    config = _base_config(bulwark={"enabled": True, "domain": "webmail.example.com", "data_dir": "/var/lib/bulwark"})
    with pytest.raises(ValueError, match="bulwark.domain"):
        validate_config(config)


def test_validate_config_allows_disabled_bulwark():
    validate_config(_base_config(bulwark={"enabled": False, "domain": "webmail.example.com", "data_dir": ""}))


def test_compose_project_name_is_unique():
    assert COMPOSE_PROJECT_NAME == "stalwart-easy-deploy"
    assert COMPOSE_PROJECT_NAME != "compose"


def test_public_url():
    assert public_url(_base_config()) == "https://mail.test.example"


def test_derive_compose_files_standalone():
    assert derive_compose_files(_base_config()) == ["docker-compose.yml", "bulwark.yml", "caddy.yml"]


def test_derive_compose_files_no_bulwark():
    assert derive_compose_files(_base_config(bulwark={"enabled": False})) == ["docker-compose.yml", "caddy.yml"]


def test_derive_compose_files_integrate():
    config = _base_config(proxy={"type": "caddy", "mode": "integrate", "integrate": {"network": "easydeploy-net"}})
    assert derive_compose_files(config) == [
        "docker-compose.yml",
        "bulwark.yml",
        "integrate.yml",
        "integrate-bulwark.yml",
    ]


def test_derive_compose_files_recovery_mode():
    config = _base_config(
        stalwart={"recovery_mode": True},
        proxy={"type": "caddy", "mode": "integrate"},
    )
    assert derive_compose_files(config)[-1] == "recovery.yml"


def test_derive_compose_files_integrate_without_bulwark():
    config = _base_config(
        bulwark={"enabled": False},
        proxy={"type": "caddy", "mode": "integrate"},
    )
    assert derive_compose_files(config) == ["docker-compose.yml", "integrate.yml"]


def test_site_blocks_include_both_hosts():
    text = site_blocks(_base_config())
    assert "mail.test.example" in text
    assert "reverse_proxy stalwart:8080" in text
    assert "proxy_protocol v2" in text
    assert "webmail.test.example" in text
    assert "reverse_proxy bulwark:3000" in text
    assert "Access-Control-Allow-Origin https://webmail.test.example" in text
    assert "Access-Control-Allow-Credentials true" in text
    assert "defer" in text
    assert "Access-Control-Expose-Headers *" not in text
    assert "header_down Access-Control-Allow-Origin " not in text


def test_site_blocks_stay_on_http_after_wizard(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "config.json").write_text("{}")
    config = _base_config(stalwart={"data_dir": str(tmp_path)})
    assert stalwart_https_upstream(config) is False
    text = site_blocks(config)
    assert "reverse_proxy stalwart:8080" in text
    assert text.index("stalwart:8080") < text.index("handle_errors 502 503")
    assert text.index("handle_errors 502 503") < text.index("https://stalwart:443")


def test_caddy_upstream_override_https():
    config = _base_config(stalwart={"caddy_upstream": "https"})
    assert stalwart_https_upstream(config) is True
    assert "https://stalwart:443" in site_blocks(config)


def test_caddy_upstream_override_http(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "config.json").write_text("{}")
    config = _base_config(stalwart={"data_dir": str(tmp_path), "caddy_upstream": "http"})
    assert stalwart_https_upstream(config) is False
    text = site_blocks(config)
    assert "reverse_proxy stalwart:8080" in text
    assert "handle_errors 502 503" in text
    assert text.index("stalwart:8080") < text.index("handle_errors 502 503")
    assert text.index("handle_errors 502 503") < text.index("https://stalwart:443")


def test_site_blocks_omit_bulwark_when_disabled():
    text = site_blocks(_base_config(bulwark={"enabled": False, "domain": "webmail.test.example"}))
    assert "mail.test.example" in text
    assert "webmail.test.example" not in text
    assert "bulwark:3000" not in text


def test_validate_config_rejects_shared_hostname():
    config = _base_config(bulwark={"enabled": True, "domain": "mail.test.example", "data_dir": "/var/lib/bulwark"})
    with pytest.raises(ValueError, match="must differ from stalwart.hostname"):
        validate_config(config)


def test_render_integration_fragment(tmp_path, monkeypatch):
    from scripts.apply import render_integration_fragment

    monkeypatch.setattr("scripts.apply.INTEGRATION_DIR", tmp_path)
    monkeypatch.setattr("scripts.apply.INTEGRATION_CADDY_FRAGMENT", tmp_path / "caddy.caddy")
    render_integration_fragment(_base_config())
    text = (tmp_path / "caddy.caddy").read_text()
    assert "mail.test.example" in text
    assert "webmail.test.example" in text


def test_render_caddyfile(tmp_path, monkeypatch):
    caddy_dir = tmp_path / "caddy"
    caddy_dir.mkdir()
    template = caddy_dir / "Caddyfile.template"
    template.write_text("{{SITE_BLOCKS}}\n")
    caddyfile = caddy_dir / "Caddyfile"
    monkeypatch.setattr("scripts.apply.CADDY_TEMPLATE", template)
    monkeypatch.setattr("scripts.apply.CADDYFILE", caddyfile)

    render_caddyfile(_base_config())
    text = caddyfile.read_text()
    assert "mail.test.example" in text
    assert "reverse_proxy stalwart:8080" in text


def test_render_template_missing_placeholder():
    with pytest.raises(ValueError, match="Unresolved"):
        render_template("{{A}}", {})


def test_load_or_create_secrets_preserves_existing(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    secrets_path = state / "secrets.yaml"
    secrets_path.write_text(yaml.safe_dump({"RECOVERY_ADMIN_PASSWORD": "keep-me"}))
    monkeypatch.setattr("scripts.apply.STATE_DIR", state)
    monkeypatch.setattr("scripts.apply.SECRETS_PATH", secrets_path)

    data = load_or_create_secrets()
    assert data["RECOVERY_ADMIN_PASSWORD"] == "keep-me"
    assert data["BULWARK_SESSION_SECRET"]


def test_load_or_create_secrets_uses_operator_password(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    secrets_path = state / "secrets.yaml"
    monkeypatch.setattr("scripts.apply.STATE_DIR", state)
    monkeypatch.setattr("scripts.apply.SECRETS_PATH", secrets_path)

    data = load_or_create_secrets(_base_config(stalwart={"recovery_admin_password": "from-wizard"}))
    assert data["RECOVERY_ADMIN_PASSWORD"] == "from-wizard"


def test_write_compose_env(tmp_path, monkeypatch):
    env_path = tmp_path / "compose.env"
    monkeypatch.setattr("scripts.apply.COMPOSE_ENV_PATH", env_path)
    monkeypatch.setattr("scripts.apply.CADDYFILE", tmp_path / "Caddyfile")
    secrets = {"RECOVERY_ADMIN_PASSWORD": "pw", "BULWARK_SESSION_SECRET": "sess"}
    write_compose_env(_base_config(), secrets)
    text = env_path.read_text()
    assert "STALWART_IMAGE=docker.io/stalwartlabs/stalwart:v0.16" in text
    assert "STALWART_PUBLIC_URL=https://mail.test.example" in text
    assert "STALWART_RECOVERY_ADMIN=admin:pw" in text
    assert "JMAP_SERVER_URL=https://mail.test.example" in text
    assert "BULWARK_SESSION_SECRET=sess" in text
    assert "SMTP_PORT=25" in text


def test_ensure_data_dirs_creates_bulwark_layout(tmp_path):
    config = _base_config(
        stalwart={"data_dir": str(tmp_path / "stalwart")},
        bulwark={
            "enabled": True,
            "domain": "webmail.test.example",
            "data_dir": str(tmp_path / "bulwark"),
        },
    )
    ensure_data_dirs(config)
    assert (tmp_path / "stalwart" / "etc").is_dir()
    assert (tmp_path / "stalwart" / "data").is_dir()
    for name in ("settings", "admin", "admin-state", "telemetry"):
        assert (tmp_path / "bulwark" / name).is_dir()

