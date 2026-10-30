"""Tests for copying Caddy's certificate onto Stalwart's mail ports."""

from __future__ import annotations

import pytest

from scripts.mail_tls import (
    caddy_container_name,
    certificate_payload,
    execute_mail_tls_plan,
    fetch_caddy_material,
    load_caddy_material,
    ordered_cert_paths,
    parse_cert_listing,
    plan_mail_tls,
    reconcile_mail_tls_timer,
    render_mail_tls_timer,
    require_hostname,
)

HOST = "mail.test.example"
CERT = "-----BEGIN CERTIFICATE-----\nY2VydA==\n-----END CERTIFICATE-----\n"
KEY = "-----BEGIN PRIVATE KEY-----\na2V5\n-----END PRIVATE KEY-----\n"
PROD = f"/data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/{HOST}/{HOST}.crt"
STAGING = (
    "/data/caddy/certificates/acme-staging-v02.api.letsencrypt.org-directory/"
    f"{HOST}/{HOST}.crt"
)


def _cert(cert_id: str, hostname: str = HOST) -> dict:
    return {"id": cert_id, **certificate_payload(hostname)}


def test_hostname_is_a_dns_name():
    assert require_hostname("Mail.Test.Example.") == HOST
    for bad in ("../etc/passwd", "mail test.example", "", "mail..example"):
        try:
            require_hostname(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_caddy_container_follows_proxy_mode():
    assert caddy_container_name("integrate") == "easydeploy_caddy"
    assert caddy_container_name("standalone") == "stalwart_caddy"


def test_production_certificate_wins_over_a_newer_staging_one():
    listing = f"200 {STAGING}\n100 {PROD}\n"
    paths = ordered_cert_paths(parse_cert_listing(listing), HOST)
    assert paths == [PROD]
    files = {PROD: CERT, PROD[:-4] + ".key": KEY, STAGING: CERT, STAGING[:-4] + ".key": KEY}
    assert load_caddy_material(listing, files, HOST) == (CERT, KEY)


def test_plan_registers_a_missing_certificate_and_selects_it():
    plan = plan_mail_tls(
        HOST,
        certificates=[],
        default_id="",
        remembered_id="",
        material_changed=True,
        have_material=True,
    )
    assert plan["create"]["certificate"]["filePath"] == f"/etc/stalwart/tls/{HOST}.crt"
    assert plan["create"]["privateKey"]["@type"] == "File"
    assert plan["set_default_to_created"] is True
    assert plan["reload_certificates"] is True
    assert plan["write"] is True


def test_plan_is_empty_when_the_default_certificate_is_already_loaded():
    plan = plan_mail_tls(
        HOST,
        certificates=[_cert("cert-1")],
        default_id="cert-1",
        remembered_id="cert-1",
        material_changed=False,
        have_material=True,
    )
    assert plan == {}


def test_plan_replaces_the_object_when_caddy_renews():
    plan = plan_mail_tls(
        HOST,
        certificates=[_cert("cert-1")],
        default_id="cert-1",
        remembered_id="cert-1",
        material_changed=True,
        have_material=True,
    )
    assert plan["destroy_ids"] == ["cert-1"]
    assert plan["create"]["certificate"]["filePath"].endswith(f"{HOST}.crt")


def test_plan_selects_an_existing_certificate_that_is_not_the_default():
    plan = plan_mail_tls(
        HOST,
        certificates=[_cert("cert-1")],
        default_id="",
        remembered_id="cert-1",
        material_changed=False,
        have_material=True,
    )
    assert plan["set_default"] == "cert-1"
    assert "create" not in plan


def test_plan_waits_when_caddy_has_no_certificate():
    assert (
        plan_mail_tls(
            HOST,
            certificates=[],
            default_id="",
            remembered_id="",
            material_changed=False,
            have_material=False,
        )
        == {}
    )


def test_execute_reloads_after_the_old_certificate_is_gone():
    calls = []

    def call(method_calls):
        calls.extend(method_calls)
        method, payload, call_id = method_calls[0]
        if method == "x:Certificate/set" and "create" in payload:
            return {"methodResponses": [[method, {"created": {"caddy-mail": {"id": "cert-2"}}}, call_id]]}
        if method == "x:Certificate/set" and "destroy" in payload:
            return {"methodResponses": [[method, {"destroyed": payload["destroy"]}, call_id]]}
        if method == "x:SystemSettings/set":
            return {"methodResponses": [[method, {"updated": {"singleton": None}}, call_id]]}
        return {"methodResponses": [[method, {"created": {"action": {"id": "1"}}}, call_id]]}

    from scripts.apply import _jmap_ok

    chosen = execute_mail_tls_plan(
        plan_mail_tls(
            HOST,
            certificates=[_cert("cert-1")],
            default_id="cert-1",
            remembered_id="cert-1",
            material_changed=True,
            have_material=True,
        ),
        call,
        _jmap_ok,
    )
    assert chosen == "cert-2"
    methods = [item[0] for item in calls]
    assert methods == [
        "x:Certificate/set",
        "x:SystemSettings/set",
        "x:Action/set",
        "x:Action/set",
        "x:Certificate/set",
        "x:Action/set",
    ]
    assert calls[1][1]["update"]["singleton"]["defaultCertificateId"] == "cert-2"
    assert calls[2][1]["create"]["settings"]["@type"] == "ReloadSettings"
    assert calls[3][1]["create"]["certs"]["@type"] == "ReloadTlsCertificates"
    assert calls[4][1]["destroy"] == ["cert-1"]
    assert calls[5][1]["create"]["certs-after"]["@type"] == "ReloadTlsCertificates"


def test_execute_restarts_when_settings_reload_is_refused():
    calls, restarts = [], []
    refused = {"type": "validationFailed", "objectId": {"object": "SpamDnsblServer"}}

    def call(method_calls):
        calls.extend(method_calls)
        method, payload, call_id = method_calls[0]
        if method == "x:Certificate/set" and "create" in payload:
            return {"methodResponses": [[method, {"created": {"caddy-mail": {"id": "cert-2"}}}, call_id]]}
        if method == "x:Certificate/set" and "destroy" in payload:
            return {"methodResponses": [[method, {"destroyed": payload["destroy"]}, call_id]]}
        if method == "x:SystemSettings/set":
            return {"methodResponses": [[method, {"updated": {"singleton": None}}, call_id]]}
        return {"methodResponses": [[method, {"notCreated": {call_id: refused}}, call_id]]}

    from scripts.apply import _jmap_ok

    plan = plan_mail_tls(
        HOST,
        certificates=[_cert("cert-1")],
        default_id="cert-1",
        remembered_id="cert-1",
        material_changed=True,
        have_material=True,
    )
    chosen = execute_mail_tls_plan(plan, call, _jmap_ok, restart=lambda: restarts.append(1) or True)
    assert chosen == "cert-2"
    assert restarts == [1]
    actions = [item[1]["create"] for item in calls if item[0] == "x:Action/set"]
    assert actions == [{"settings": {"@type": "ReloadSettings"}}]

    with pytest.raises(RuntimeError, match="Could not run ReloadSettings"):
        execute_mail_tls_plan(plan, call, _jmap_ok)


def test_caddy_lookup_passes_the_hostname_only_as_a_filter():
    commands = []

    def run(args, **kwargs):
        del kwargs
        commands.append(args)
        if args[3] == "sh":
            return type("R", (), {"returncode": 0, "stdout": f"10 {PROD}\n", "stderr": ""})()
        if args[-1].endswith(".crt"):
            return type("R", (), {"returncode": 0, "stdout": CERT, "stderr": ""})()
        if args[-1].endswith(".key"):
            return type("R", (), {"returncode": 0, "stdout": KEY, "stderr": ""})()
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "missing"})()

    assert fetch_caddy_material(HOST, "easydeploy_caddy", run=run) == (CERT, KEY)
    assert HOST not in commands[0][4]


def test_mail_tls_timer_is_hourly(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYDEPLOY_ASSUME_SYSTEMD", "1")
    runs = []

    def run_command(command, check):
        del check
        runs.append(command)

    message = reconcile_mail_tls_timer(project_root=tmp_path, unit_dir=tmp_path, run_command=run_command)
    assert message == "Mail certificate renewal timer installed or updated."
    timer = (tmp_path / "stalwart-easy-deploy-mail-tls.timer").read_text()
    assert timer == render_mail_tls_timer("stalwart-easy-deploy-mail-tls")
    assert "OnCalendar=hourly" in timer
    service = (tmp_path / "stalwart-easy-deploy-mail-tls.service").read_text()
    assert "scripts.mail_tls" in service
    assert ["systemctl", "enable", "--now", "stalwart-easy-deploy-mail-tls.timer"] in runs
