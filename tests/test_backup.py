"""Tests for Stalwart's shared backup wiring."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import apply as apply_module
from scripts.apply import (
    PROJECT_ROOT,
    backup_secret_keys,
    load_or_create_secrets,
    reconcile_backup_schedule,
    validate_backup_config,
)
from scripts.backup_plan import resolve_plan


def _write_deploy(tmp_path: Path, config: dict) -> Path:
    path = tmp_path / "deploy.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def test_resolve_plan_follows_configured_data_dirs(tmp_path: Path):
    _write_deploy(
        tmp_path,
        {
            "stalwart": {"data_dir": "/srv/mail"},
            "bulwark": {"enabled": True, "data_dir": "/srv/webmail"},
        },
    )

    plan = resolve_plan(tmp_path)

    assert plan["service"] == "stalwart"
    assert plan["archive_prefix"] == "stalwart"
    assert plan["timer_name"] == "stalwart-easy-deploy-backup"
    assert plan["state_dir"] == ".stalwart-easy-deploy"
    assert plan["secrets_file"] == ".stalwart-easy-deploy/secrets.yaml"
    assert plan["hooks"] == {"apply": "apply.sh", "start": "start.sh", "stop": "stop.sh"}
    assert plan["persistent_paths"] == [
        {"path": "deploy.yaml", "as": "deploy.yaml"},
        {"path": ".stalwart-easy-deploy", "as": ".stalwart-easy-deploy"},
        {"path": "/srv/mail", "as": "data/stalwart"},
        {"path": "/srv/webmail", "as": "data/bulwark"},
    ]


def test_resolve_plan_omits_disabled_bulwark(tmp_path: Path):
    _write_deploy(tmp_path, {"bulwark": {"enabled": False, "data_dir": "/srv/webmail"}})
    plan = resolve_plan(tmp_path)
    assert [entry["as"] for entry in plan["persistent_paths"]] == [
        "deploy.yaml",
        ".stalwart-easy-deploy",
        "data/stalwart",
    ]


def test_shared_loader_emits_dynamic_plan():
    sys.path.insert(0, str(PROJECT_ROOT / "easydeploy-lib" / "python"))
    try:
        import backup_plan

        plan = backup_plan.load_plan(PROJECT_ROOT)
    finally:
        sys.path.pop(0)
    assert plan["timer_name"] == "stalwart-easy-deploy-backup"
    assert plan["databases"] == []
    assert plan["docker_volumes"] == []


def test_backup_secret_keys_and_generation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(apply_module, "SECRETS_PATH", tmp_path / "secrets.yaml")
    assert backup_secret_keys({"backup": {"enabled": True}}) == ("BORG_PASSPHRASE",)
    assert backup_secret_keys({}) == ()

    generated = load_or_create_secrets({"backup": {"enabled": True}})
    assert generated["BORG_PASSPHRASE"]
    assert load_or_create_secrets({"backup": {"enabled": True}})["BORG_PASSPHRASE"] == generated[
        "BORG_PASSPHRASE"
    ]


def test_backup_secret_not_generated_when_disabled(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(apply_module, "SECRETS_PATH", tmp_path / "secrets.yaml")
    assert "BORG_PASSPHRASE" not in load_or_create_secrets({})


def test_validate_backup_config_rejects_relative_local_path(tmp_path: Path):
    path = _write_deploy(
        tmp_path,
        {"backup": {"enabled": True, "repository": {"type": "local", "path": "relative"}}},
    )
    with pytest.raises(ValueError, match="absolute path"):
        validate_backup_config(path)


def test_validate_backup_config_rejects_missing_sftp_key(tmp_path: Path):
    path = _write_deploy(
        tmp_path,
        {
            "backup": {
                "enabled": True,
                "repository": {
                    "type": "sftp",
                    "host": "backup.example.com",
                    "user": "borg",
                    "path": "/repo",
                    "ssh_key_path": str(tmp_path / "missing-key"),
                },
            }
        },
    )
    with pytest.raises(ValueError, match="ssh_key_path not found"):
        validate_backup_config(path)


def test_apply_reconciles_schedule_via_backup_script(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="Automatic backup timer installed.\n", stderr="")

    monkeypatch.setattr(apply_module.subprocess, "run", fake_run)
    reconcile_backup_schedule({"backup": {"enabled": True}})

    assert calls == [
        (
            ["bash", str(PROJECT_ROOT / "backup.sh"), "--schedule"],
            {
                "cwd": PROJECT_ROOT,
                "check": True,
                "capture_output": True,
                "text": True,
            },
        )
    ]


def _run_script(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(PROJECT_ROOT / script), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def test_backup_help_and_unknown_argument():
    result = _run_script("backup.sh", "--help")
    assert result.returncode == 0
    assert "Usage:" in result.stdout

    result = _run_script("backup.sh", "--unknown")
    assert result.returncode == 1
    assert "Unknown flag" in result.stderr


def test_restore_requires_source():
    result = _run_script("restore.sh")
    assert result.returncode == 1
    assert "Choose one source" in result.stderr


def test_bootstrap_help():
    result = _run_script("bootstrap-from-backup.sh", "--help")
    assert result.returncode == 0
    assert "portable backup file" in result.stdout
