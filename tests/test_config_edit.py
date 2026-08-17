"""Tests for scripts/config_edit.py."""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.config_edit import update_from_wizard


def test_update_from_wizard_writes_deploy_yaml(tmp_path: Path):
    path = tmp_path / "deploy.yaml"
    example = tmp_path / "deploy.yaml.example"
    example.write_text("stalwart:\n  image: docker.io/stalwartlabs/stalwart\n")

    # load_or_init looks next to PROJECT_ROOT; write directly then call with path
    update_from_wizard(
        hostname="mail.example.org",
        domain="example.org",
        data_dir="/data/stalwart",
        bulwark_enabled=True,
        bulwark_domain="webmail.example.org",
        bulwark_data_dir="/data/bulwark",
        recovery_admin_password="s3cret",
        proxy_mode="integrate",
        path=path,
    )
    data = yaml.safe_load(path.read_text())
    assert data["stalwart"]["hostname"] == "mail.example.org"
    assert data["stalwart"]["domain"] == "example.org"
    assert data["stalwart"]["data_dir"] == "/data/stalwart"
    assert data["stalwart"]["recovery_admin_password"] == "s3cret"
    assert data["bulwark"]["enabled"] is True
    assert data["bulwark"]["domain"] == "webmail.example.org"
    assert data["proxy"]["mode"] == "integrate"
    assert data["proxy"]["integrate"]["network"] == "easydeploy-net"
    assert data["ports"]["smtp"] == 25


def test_update_from_wizard_omits_empty_password(tmp_path: Path):
    path = tmp_path / "deploy.yaml"
    update_from_wizard(
        hostname="mail.example.org",
        domain="example.org",
        data_dir="/data/stalwart",
        bulwark_enabled=False,
        bulwark_domain="webmail.example.org",
        bulwark_data_dir="/data/bulwark",
        recovery_admin_password=None,
        proxy_mode="standalone",
        path=path,
    )
    data = yaml.safe_load(path.read_text())
    assert "recovery_admin_password" not in data["stalwart"]
    assert data["bulwark"]["enabled"] is False
    assert data["proxy"]["mode"] == "standalone"
