"""Tests for scripts/dns_records.py."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.dns_records import dns_records, main


def test_dns_records_leave_dkim_pending_until_a_key_exists():
    payload = dns_records(
        {
            "stalwart": {"hostname": "mail.example.com", "domain": "example.com"},
            "bulwark": {"enabled": True, "domain": "webmail.example.com"},
        }
    )

    by_purpose = {record["purpose"]: record for record in payload["records"]}
    assert payload["version"] == 1
    assert payload["mail_hostname"] == "mail.example.com"
    assert by_purpose["mx"] == {
        "purpose": "mx",
        "type": "MX",
        "name": "example.com",
        "priority": 10,
        "value": "mail.example.com",
    }
    assert by_purpose["spf"]["value"] == "v=spf1 mx -all"
    assert by_purpose["dmarc"]["name"] == "_dmarc.example.com"
    assert by_purpose["dkim"]["status"] == "pending"
    assert by_purpose["dkim"]["value"] is None
    assert by_purpose["mail-a"]["fill_from"] == "server_ipv4"
    assert by_purpose["webmail-a"]["name"] == "webmail.example.com"
    assert by_purpose["ptr"]["value"] == "mail.example.com"


def test_dns_records_include_a_supplied_dkim_value(tmp_path: Path, capsys):
    config = tmp_path / "deploy.yaml"
    config.write_text(
        "stalwart:\n  hostname: mail.example.com\n  domain: example.com\n"
        "bulwark:\n  enabled: false\n"
    )

    code = main(
        [
            "--config",
            str(config),
            "--dkim-txt",
            "v=DKIM1; k=rsa; p=abc",
            "--compact",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    dkim = next(record for record in payload["records"] if record["purpose"] == "dkim")
    assert dkim["status"] == "ready"
    assert dkim["value"] == "v=DKIM1; k=rsa; p=abc"
    assert "webmail-a" not in {record["purpose"] for record in payload["records"]}
