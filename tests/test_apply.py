"""Tests for scripts/apply.py."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.apply import (
    COMPOSE_PROJECT_NAME,
    address_is_docker_lan,
    bootstrap_update_fields,
    derive_compose_files,
    ensure_data_dirs,
    load_or_create_secrets,
    protect_caddy_from_autoban,
    public_url,
    render_caddyfile,
    render_template,
    site_blocks,
    stalwart_https_upstream,
    validate_config,
    write_compose_env,
    _jmap_ok,
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


def test_address_is_docker_lan():
    assert address_is_docker_lan("172.19.0.5") is True
    assert address_is_docker_lan("172.16.0.0/12") is True
    assert address_is_docker_lan("10.0.0.1") is True
    assert address_is_docker_lan("8.8.8.8") is False


def test_parse_jmap_ok():
    payload = _jmap_ok(
        {
            "methodResponses": [
                ["x:BlockedIp/query", {"ids": ["ban1"]}, "q"],
            ]
        },
        "q",
    )
    assert payload["ids"] == ["ban1"]


def test_bootstrap_update_fields():
    fields = bootstrap_update_fields(_base_config())
    assert fields["serverHostname"] == "mail.test.example"
    assert fields["defaultDomain"] == "test.example"
    assert fields["requestTlsCertificate"] is False
    assert fields["generateDkimKeys"] is True
    assert fields["tracer"]["@type"] == "Stdout"


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
    assert "proxy_protocol" not in text
    assert "webmail.test.example" in text
    assert "reverse_proxy bulwark:3000" in text
    assert "Access-Control-Allow-Origin https://webmail.test.example" in text
    assert "Access-Control-Allow-Credentials true" in text
    assert "defer" in text
    assert "Access-Control-Expose-Headers *" not in text
    assert "header_down Access-Control-Allow-Origin " not in text
    assert "@scan_ban" in text
    assert "handle @scan_ban" in text
    assert "handle_errors" not in text
    assert "https://stalwart:443" not in text


def test_site_blocks_stay_on_http_after_wizard(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "config.json").write_text("{}")
    config = _base_config(stalwart={"data_dir": str(tmp_path)})
    assert stalwart_https_upstream(config) is False
    text = site_blocks(config)
    assert "reverse_proxy stalwart:8080" in text
    assert "handle_errors" not in text
    assert "https://stalwart:443" not in text


def test_caddy_upstream_override_https():
    config = _base_config(stalwart={"caddy_upstream": "https"})
    assert stalwart_https_upstream(config) is True
    text = site_blocks(config)
    assert "https://stalwart:443" in text
    assert "stalwart:8080" not in text
    assert "handle_errors" not in text


def test_caddy_upstream_override_http(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "config.json").write_text("{}")
    config = _base_config(stalwart={"data_dir": str(tmp_path), "caddy_upstream": "http"})
    assert stalwart_https_upstream(config) is False
    text = site_blocks(config)
    assert "reverse_proxy stalwart:8080" in text
    assert "handle_errors" not in text
    assert "https://stalwart:443" not in text


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


def test_protect_caddy_unbans_docker_lan_and_enables_forwarded(monkeypatch):
    inspect = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    calls: list[list] = []

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["docker", "inspect"]:
            fmt = " ".join(cmd)
            if "Networks" in fmt:
                return type("R", (), {"returncode": 0, "stdout": "easydeploy-net\n", "stderr": ""})()
            return inspect
        if cmd[:3] == ["docker", "network", "inspect"]:
            return type("R", (), {"returncode": 0, "stdout": "172.19.0.0/16\n", "stderr": ""})()
        raise AssertionError(cmd)

    def fake_jmap(_config, _secrets, method_calls):
        calls.append(method_calls)
        method = method_calls[0][0]
        cid = method_calls[0][2]
        if method == "x:BlockedIp/query":
            return {"methodResponses": [[method, {"ids": ["ban1", "ban2"]}, cid]]}
        if method == "x:BlockedIp/get":
            return {
                "methodResponses": [
                    [
                        method,
                        {
                            "list": [
                                {"id": "ban1", "address": "172.19.0.5"},
                                {"id": "ban2", "address": "8.8.8.8"},
                            ]
                        },
                        cid,
                    ]
                ]
            }
        if method == "x:BlockedIp/set":
            return {"methodResponses": [[method, {"destroyed": ["ban1"]}, cid]]}
        if method == "x:AllowedIp/query":
            return {"methodResponses": [[method, {"ids": []}, cid]]}
        if method == "x:AllowedIp/get":
            return {"methodResponses": [[method, {"list": []}, cid]]}
        if method == "x:AllowedIp/set":
            return {"methodResponses": [[method, {"created": method_calls[0][1]["create"]}, cid]]}
        if method == "x:Http/set":
            return {"methodResponses": [[method, {"updated": {"singleton": None}}, cid]]}
        if method == "x:Security/set":
            return {"methodResponses": [[method, {"updated": {"singleton": None}}, cid]]}
        if method == "x:Action/set":
            key = next(iter(method_calls[0][1]["create"]))
            return {
                "methodResponses": [
                    [method, {"created": {key: {"id": f"action-{key}"}}}, cid]
                ]
            }
        raise AssertionError(method)

    monkeypatch.setattr("scripts.apply.subprocess.run", fake_run)
    monkeypatch.setattr("scripts.apply._jmap", fake_jmap)

    protect_caddy_from_autoban(_base_config(), {"RECOVERY_ADMIN_PASSWORD": "pw"})
    destroy = next(c[0][1] for c in calls if c[0][0] == "x:BlockedIp/set")
    assert destroy["destroy"] == ["ban1"]
    create = next(c[0][1] for c in calls if c[0][0] == "x:AllowedIp/set")
    created_addrs = {body["address"] for body in create["create"].values()}
    assert "172.16.0.0/12" in created_addrs
    assert "172.19.0.0/16" in created_addrs
    http_update = next(c[0][1] for c in calls if c[0][0] == "x:Http/set")
    assert http_update["update"]["singleton"]["useXForwarded"] is True
    security = next(c[0][1] for c in calls if c[0][0] == "x:Security/set")
    assert security["update"]["singleton"]["scanBanPaths"] == {}
    actions = [
        next(iter(c[0][1]["create"].values()))["@type"]
        for c in calls
        if c[0][0] == "x:Action/set"
    ]
    assert actions == ["ReloadSettings", "ReloadBlockedIps"]


def test_protect_caddy_completes_bootstrap(tmp_path, monkeypatch):
    secrets_path = tmp_path / "secrets.yaml"
    list_calls = {"n": 0}

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["docker", "inspect"] or cmd[:2] == ["docker", "restart"]:
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if cmd[:3] == ["docker", "network", "inspect"]:
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        raise AssertionError(cmd)

    def fake_list(_config, _secrets, type_name):
        list_calls["n"] += 1
        if list_calls["n"] == 1:
            raise RuntimeError(
                "The server is in bootstrap mode. Only the 'Bootstrap' object type "
                "can be accessed until the bootstrap process is complete."
            )
        if type_name == "BlockedIp":
            return [{"id": "ban1", "address": "172.19.0.5"}]
        return []

    def fake_jmap(_config, _secrets, method_calls, **_kwargs):
        method = method_calls[0][0]
        cid = method_calls[0][2]
        if method == "x:Bootstrap/set":
            body = method_calls[0][1]["update"]["singleton"]
            assert body["serverHostname"] == "mail.test.example"
            assert body["requestTlsCertificate"] is False
            return {
                "methodResponses": [
                    [
                        method,
                        {
                            "updated": {
                                "singleton": {
                                    "username": "admin@test.example",
                                    "secret": "genpw",
                                }
                            }
                        },
                        cid,
                    ]
                ]
            }
        if method == "x:BlockedIp/set":
            return {"methodResponses": [[method, {"destroyed": ["ban1"]}, cid]]}
        if method == "x:AllowedIp/set":
            return {"methodResponses": [[method, {"created": {"docker-lan": {"id": "a1"}}}, cid]]}
        if method == "x:Http/set":
            return {"methodResponses": [[method, {"updated": {"singleton": None}}, cid]]}
        if method == "x:Security/set":
            return {"methodResponses": [[method, {"updated": {"singleton": None}}, cid]]}
        if method == "x:Action/set":
            key = next(iter(method_calls[0][1]["create"]))
            return {
                "methodResponses": [
                    [method, {"created": {key: {"id": f"action-{key}"}}}, cid]
                ]
            }
        raise AssertionError(method)

    monkeypatch.setattr("scripts.apply.subprocess.run", fake_run)
    monkeypatch.setattr("scripts.apply._jmap_list", fake_list)
    monkeypatch.setattr("scripts.apply._jmap", fake_jmap)
    monkeypatch.setattr("scripts.apply.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr("scripts.apply.SECRETS_PATH", secrets_path)

    protect_caddy_from_autoban(_base_config(), {"RECOVERY_ADMIN_PASSWORD": "pw"})
    saved = yaml.safe_load(secrets_path.read_text())
    assert saved["STALWART_ADMIN_PASSWORD"] == "genpw"
    assert saved["STALWART_ADMIN_USER"] == "admin@test.example"

