"""Tests for scripts/apply.py."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.apply import (
    COMPOSE_PROJECT_NAME,
    address_is_docker_lan,
    apply_engine_identity_sidecar,
    apply_kanidm_directory,
    bootstrap_update_fields,
    build_ldap_directory,
    build_oidc_directory,
    bulwark_oauth_env_lines,
    _directory_matches,
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
            "tag": "v0.16.20",
            "data_dir": "/var/lib/stalwart",
        },
        "bulwark": {
            "enabled": True,
            "domain": "webmail.test.example",
            "image": "ghcr.io/bulwarkmail/webmail",
            "tag": "1.9.2",
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
    monkeypatch.setattr("scripts.apply.IDENTITY_SIDECAR", tmp_path / "missing-identity.yaml")
    secrets = {"RECOVERY_ADMIN_PASSWORD": "pw", "BULWARK_SESSION_SECRET": "sess"}
    write_compose_env(_base_config(), secrets)
    text = env_path.read_text()
    assert "STALWART_IMAGE=docker.io/stalwartlabs/stalwart:v0.16.20" in text
    assert "STALWART_PUBLIC_URL=https://mail.test.example" in text
    assert "STALWART_RECOVERY_ADMIN=admin:pw" in text
    assert "JMAP_SERVER_URL=https://mail.test.example" in text
    assert "BULWARK_SESSION_SECRET=sess" in text
    assert "SMTP_PORT=25" in text
    assert "OAUTH_ENABLED=" not in text


def test_bulwark_oauth_env_lines_from_sidecar(tmp_path, monkeypatch):
    sidecar = tmp_path / "identity-provider.yaml"
    sidecar.write_text(
        yaml.safe_dump(
            {
                "provider": "kanidm",
                "oidc": {
                    "issuer_url": "https://idm.test.example/oauth2/openid/stalwart-webui",
                    "client_id": "stalwart-webui",
                },
            }
        )
    )
    monkeypatch.setattr("scripts.apply.IDENTITY_SIDECAR", sidecar)
    lines = bulwark_oauth_env_lines(_base_config())
    assert "OAUTH_ENABLED=true" in lines
    assert "OAUTH_ONLY=true" in lines
    assert "AUTO_SSO_ENABLED=true" in lines
    assert "OAUTH_CLIENT_ID=stalwart-webui" in lines
    assert "OAUTH_ISSUER_URL=https://idm.test.example/oauth2/openid/stalwart-webui" in lines


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


def test_apply_engine_identity_sidecar_fills_kanidm(tmp_path):
    sidecar = tmp_path / "identity-provider.yaml"
    sidecar.write_text(
        yaml.safe_dump(
            {
                "provider": "kanidm",
                "domain": "idm.test.example",
                "ldap": {
                    "url": "ldaps://kanidm:3636",
                    "base_dn": "dc=idm,dc=test,dc=example",
                    "bind_dn": "dn=token",
                    "bind_secret": "ldap-token",
                },
                "oidc": {
                    "issuer_url": "https://idm.test.example/oauth2/openid/stalwart-webui",
                    "client_id": "stalwart-webui",
                },
            }
        )
    )
    config = {"identity": {}}
    identity = apply_engine_identity_sidecar(config, sidecar)
    assert identity["provider"] == "kanidm"
    assert identity["ldap"]["base_dn"] == "dc=idm,dc=test,dc=example"
    assert identity["ldap"]["bind_secret"] == "ldap-token"


def test_apply_engine_identity_sidecar_respects_external_provider(tmp_path):
    sidecar = tmp_path / "identity-provider.yaml"
    sidecar.write_text(yaml.safe_dump({"provider": "kanidm", "domain": "idm.test.example"}))
    config = {"identity": {"provider": "internal"}}
    identity = apply_engine_identity_sidecar(config, sidecar)
    assert identity["provider"] == "internal"
    assert "ldap" not in identity


def test_build_ldap_directory_payload():
    identity = {
        "provider": "kanidm",
        "ldap": {
            "url": "ldaps://kanidm:3636",
            "base_dn": "dc=idm,dc=test,dc=example",
            "bind_secret": "token",
        },
    }
    payload = build_ldap_directory(identity, {})
    assert payload is not None
    assert payload["@type"] == "Ldap"
    assert payload["baseDn"] == "dc=idm,dc=test,dc=example"
    assert payload["bindSecret"] == {"@type": "Value", "secret": "token"}
    assert payload["bindAuthentication"] is True
    assert "objectclass=account" in payload["filterLogin"]
    assert "spn=?" in payload["filterLogin"]
    assert "mail=?" in payload["filterMailbox"]


def test_build_oidc_directory_payload():
    identity = {
        "provider": "kanidm",
        "oidc": {
            "issuer_url": "https://idm.test.example/oauth2/openid/stalwart-webui",
            "client_id": "stalwart-webui",
            "username_domain": "test.example",
        },
    }
    payload = build_oidc_directory(identity)
    assert payload is not None
    assert payload["@type"] == "Oidc"
    assert payload["issuerUrl"].endswith("/oauth2/openid/stalwart-webui")
    assert payload["requireAudience"] == "stalwart-webui"
    assert payload["usernameDomain"] == "test.example"
    assert payload["claimUsername"] == "preferred_username"


def test_apply_kanidm_directory_creates_and_selects(tmp_path, monkeypatch):
    sidecar = tmp_path / "identity-provider.yaml"
    sidecar.write_text(
        yaml.safe_dump(
            {
                "provider": "kanidm",
                "ldap": {
                    "url": "ldaps://kanidm:3636",
                    "base_dn": "dc=idm,dc=test,dc=example",
                    "bind_secret": "token",
                },
            }
        )
    )
    secrets_path = tmp_path / "secrets.yaml"
    calls: list[str] = []

    def fake_list(_config, _secrets, type_name):
        assert type_name == "Directory"
        return []

    def fake_jmap(_config, _secrets, method_calls, **_kwargs):
        method = method_calls[0][0]
        calls.append(method)
        cid = method_calls[0][2]
        if method == "x:Directory/set":
            return {
                "methodResponses": [
                    [method, {"created": {"kanidm": {"id": "dir-kanidm"}}}, cid]
                ]
            }
        if method == "x:Authentication/set":
            assert method_calls[0][1]["update"]["singleton"]["directoryId"] == "dir-kanidm"
            return {"methodResponses": [[method, {"updated": {"singleton": None}}, cid]]}
        raise AssertionError(method)

    monkeypatch.setattr("scripts.apply.IDENTITY_SIDECAR", sidecar)
    monkeypatch.setattr("scripts.apply.SECRETS_PATH", secrets_path)
    monkeypatch.setattr("scripts.apply._jmap_list", fake_list)
    monkeypatch.setattr("scripts.apply._jmap", fake_jmap)

    secrets: dict = {}
    apply_kanidm_directory(_base_config(), secrets)
    assert "x:Directory/set" in calls
    assert "x:Authentication/set" in calls
    assert secrets["KANIDM_DIRECTORY_ID"] == "dir-kanidm"


def test_apply_kanidm_directory_selects_oidc_for_sso(tmp_path, monkeypatch):
    sidecar = tmp_path / "identity-provider.yaml"
    sidecar.write_text(
        yaml.safe_dump(
            {
                "provider": "kanidm",
                "auth_directory": "oidc",
                "ldap": {
                    "url": "ldaps://kanidm:3636",
                    "base_dn": "dc=idm,dc=test,dc=example",
                    "bind_secret": "token",
                },
                "oidc": {
                    "issuer_url": "https://idm.test.example/oauth2/openid/stalwart-webui",
                    "client_id": "stalwart-webui",
                    "username_domain": "test.example",
                },
            }
        )
    )
    secrets_path = tmp_path / "secrets.yaml"
    created: list[str] = []
    selected: list[str] = []
    directories: list[dict] = []

    def fake_list(_config, _secrets, type_name):
        assert type_name == "Directory"
        return list(directories)

    def fake_jmap(_config, _secrets, method_calls, **_kwargs):
        method = method_calls[0][0]
        cid = method_calls[0][2]
        if method == "x:Directory/set":
            create = method_calls[0][1].get("create") or {}
            key = next(iter(create))
            created.append(key)
            new_id = f"dir-{key}"
            directories.append({"id": new_id, "description": create[key]["description"]})
            return {"methodResponses": [[method, {"created": {key: {"id": new_id}}}, cid]]}
        if method == "x:Authentication/set":
            selected.append(method_calls[0][1]["update"]["singleton"]["directoryId"])
            return {"methodResponses": [[method, {"updated": {"singleton": None}}, cid]]}
        raise AssertionError(method)

    monkeypatch.setattr("scripts.apply.IDENTITY_SIDECAR", sidecar)
    monkeypatch.setattr("scripts.apply.SECRETS_PATH", secrets_path)
    monkeypatch.setattr("scripts.apply._jmap_list", fake_list)
    monkeypatch.setattr("scripts.apply._jmap", fake_jmap)

    secrets: dict = {}
    apply_kanidm_directory(_base_config(), secrets)
    assert created == ["kanidm", "kanidmOidc"]
    assert selected == ["dir-kanidmOidc"]
    assert secrets["KANIDM_DIRECTORY_ID"] == "dir-kanidmOidc"
    assert secrets["KANIDM_LDAP_DIRECTORY_ID"] == "dir-kanidm"
    assert secrets["KANIDM_OIDC_DIRECTORY_ID"] == "dir-kanidmOidc"


def test_apply_kanidm_directory_defaults_to_oidc_when_present(tmp_path, monkeypatch):
    sidecar = tmp_path / "identity-provider.yaml"
    sidecar.write_text(
        yaml.safe_dump(
            {
                "provider": "kanidm",
                "ldap": {
                    "url": "ldaps://kanidm:3636",
                    "base_dn": "dc=idm,dc=test,dc=example",
                    "bind_secret": "token",
                },
                "oidc": {
                    "issuer_url": "https://idm.test.example/oauth2/openid/stalwart-webui",
                    "client_id": "stalwart-webui",
                },
            }
        )
    )
    secrets_path = tmp_path / "secrets.yaml"
    selected: list[str] = []
    directories: list[dict] = []

    def fake_list(_config, _secrets, type_name):
        return list(directories)

    def fake_jmap(_config, _secrets, method_calls, **_kwargs):
        method = method_calls[0][0]
        cid = method_calls[0][2]
        if method == "x:Directory/set":
            create = method_calls[0][1].get("create") or {}
            key = next(iter(create))
            new_id = f"dir-{key}"
            directories.append({"id": new_id, "description": create[key]["description"]})
            return {"methodResponses": [[method, {"created": {key: {"id": new_id}}}, cid]]}
        if method == "x:Authentication/set":
            selected.append(method_calls[0][1]["update"]["singleton"]["directoryId"])
            return {"methodResponses": [[method, {"updated": {"singleton": None}}, cid]]}
        raise AssertionError(method)

    monkeypatch.setattr("scripts.apply.IDENTITY_SIDECAR", sidecar)
    monkeypatch.setattr("scripts.apply.SECRETS_PATH", secrets_path)
    monkeypatch.setattr("scripts.apply._jmap_list", fake_list)
    monkeypatch.setattr("scripts.apply._jmap", fake_jmap)

    secrets: dict = {}
    apply_kanidm_directory(_base_config(), secrets)
    assert selected == ["dir-kanidmOidc"]
    assert secrets["KANIDM_DIRECTORY_ID"] == "dir-kanidmOidc"


def test_directory_matches_ldap_by_url_when_description_is_generic():
    payload = {"@type": "Ldap", "description": "Kanidm", "url": "ldaps://kanidm:3636"}
    assert _directory_matches({"url": "ldaps://kanidm:3636", "description": "LDAP Directory"}, payload)
    assert _directory_matches({"issuerUrl": "ldaps://kanidm:3636"}, payload)
    assert not _directory_matches({"url": "ldaps://other:3636"}, payload)


def test_directory_matches_oidc_by_issuer():
    payload = {
        "@type": "Oidc",
        "description": "Kanidm OIDC",
        "issuerUrl": "https://auth.opencomp.eu/oauth2/openid/stalwart-webui",
    }
    assert _directory_matches(
        {"issuerUrl": "https://auth.opencomp.eu/oauth2/openid/stalwart-webui"},
        payload,
    )
    assert not _directory_matches(
        {"issuerUrl": "https://auth.opencomp.eu/oauth2/openid/other"},
        payload,
    )


def test_apply_kanidm_directory_reuses_url_and_destroys_duplicates(tmp_path, monkeypatch):
    sidecar = tmp_path / "identity-provider.yaml"
    sidecar.write_text(
        yaml.safe_dump(
            {
                "provider": "kanidm",
                "ldap": {
                    "url": "ldaps://kanidm:3636",
                    "base_dn": "dc=idm,dc=test,dc=example",
                    "bind_secret": "token",
                },
                "oidc": {
                    "issuer_url": "https://idm.test.example/oauth2/openid/stalwart-webui",
                    "client_id": "stalwart-webui",
                },
            }
        )
    )
    secrets_path = tmp_path / "secrets.yaml"
    destroyed: list[str] = []
    created: list[str] = []
    directories = [
        {"id": "ldap-1", "url": "ldaps://kanidm:3636", "description": "LDAP Directory"},
        {"id": "ldap-2", "url": "ldaps://kanidm:3636", "description": "LDAP Directory"},
        {"id": "ldap-3", "issuerUrl": "ldaps://kanidm:3636"},
        {
            "id": "oidc-1",
            "issuerUrl": "https://idm.test.example/oauth2/openid/stalwart-webui",
            "description": "Kanidm OIDC",
        },
    ]

    def fake_list(_config, _secrets, type_name):
        return list(directories)

    def fake_jmap(_config, _secrets, method_calls, **_kwargs):
        method = method_calls[0][0]
        args = method_calls[0][1]
        cid = method_calls[0][2]
        if method == "x:Directory/set":
            if args.get("destroy"):
                destroyed.extend(args["destroy"])
                keep = set(args["destroy"])
                directories[:] = [item for item in directories if item.get("id") not in keep]
                return {"methodResponses": [[method, {"destroyed": args["destroy"]}, cid]]}
            if args.get("update"):
                return {"methodResponses": [[method, {"updated": args["update"]}, cid]]}
            create = args.get("create") or {}
            key = next(iter(create))
            created.append(key)
            new_id = f"dir-{key}"
            directories.append({"id": new_id, "description": create[key]["description"]})
            return {"methodResponses": [[method, {"created": {key: {"id": new_id}}}, cid]]}
        if method == "x:Authentication/set":
            return {"methodResponses": [[method, {"updated": {"singleton": None}}, cid]]}
        raise AssertionError(method)

    monkeypatch.setattr("scripts.apply.IDENTITY_SIDECAR", sidecar)
    monkeypatch.setattr("scripts.apply.SECRETS_PATH", secrets_path)
    monkeypatch.setattr("scripts.apply._jmap_list", fake_list)
    monkeypatch.setattr("scripts.apply._jmap", fake_jmap)

    secrets: dict = {}
    apply_kanidm_directory(_base_config(), secrets)
    assert created == []
    assert set(destroyed) == {"ldap-2", "ldap-3"}
    assert secrets["KANIDM_DIRECTORY_ID"] == "oidc-1"
    assert secrets["KANIDM_OIDC_DIRECTORY_ID"] == "oidc-1"
    assert secrets["KANIDM_LDAP_DIRECTORY_ID"] == "ldap-1"


def test_apply_kanidm_directory_operator_can_keep_ldap(tmp_path, monkeypatch):
    sidecar = tmp_path / "identity-provider.yaml"
    sidecar.write_text(
        yaml.safe_dump(
            {
                "provider": "kanidm",
                "auth_directory": "oidc",
                "ldap": {
                    "url": "ldaps://kanidm:3636",
                    "base_dn": "dc=idm,dc=test,dc=example",
                    "bind_secret": "token",
                },
                "oidc": {
                    "issuer_url": "https://idm.test.example/oauth2/openid/stalwart-webui",
                    "client_id": "stalwart-webui",
                },
            }
        )
    )
    secrets_path = tmp_path / "secrets.yaml"
    selected: list[str] = []
    directories: list[dict] = []

    def fake_list(_config, _secrets, type_name):
        return list(directories)

    def fake_jmap(_config, _secrets, method_calls, **_kwargs):
        method = method_calls[0][0]
        cid = method_calls[0][2]
        if method == "x:Directory/set":
            create = method_calls[0][1].get("create") or {}
            key = next(iter(create))
            new_id = f"dir-{key}"
            directories.append({"id": new_id, "description": create[key]["description"]})
            return {"methodResponses": [[method, {"created": {key: {"id": new_id}}}, cid]]}
        if method == "x:Authentication/set":
            selected.append(method_calls[0][1]["update"]["singleton"]["directoryId"])
            return {"methodResponses": [[method, {"updated": {"singleton": None}}, cid]]}
        raise AssertionError(method)

    monkeypatch.setattr("scripts.apply.IDENTITY_SIDECAR", sidecar)
    monkeypatch.setattr("scripts.apply.SECRETS_PATH", secrets_path)
    monkeypatch.setattr("scripts.apply._jmap_list", fake_list)
    monkeypatch.setattr("scripts.apply._jmap", fake_jmap)

    secrets: dict = {}
    apply_kanidm_directory(_base_config(identity={"auth_directory": "ldap"}), secrets)
    assert selected == ["dir-kanidm"]
    assert secrets["KANIDM_DIRECTORY_ID"] == "dir-kanidm"


def test_apply_kanidm_directory_missing_bind_secret_raises(tmp_path, monkeypatch):
    sidecar = tmp_path / "identity-provider.yaml"
    sidecar.write_text(
        yaml.safe_dump(
            {
                "provider": "kanidm",
                "ldap": {
                    "url": "ldaps://kanidm:3636",
                    "base_dn": "dc=idm,dc=test,dc=example",
                },
            }
        )
    )
    monkeypatch.setattr("scripts.apply.IDENTITY_SIDECAR", sidecar)
    monkeypatch.setattr("scripts.apply._sibling_kanidm_ldap_token", lambda: "")
    with pytest.raises(RuntimeError, match="LDAP bind secret"):
        apply_kanidm_directory(_base_config(), {})

