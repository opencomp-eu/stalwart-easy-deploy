"""Tests for the standard kit update.sh contract."""

from __future__ import annotations

import sys

from scripts.update import main, update_spec


def test_update_spec_lock_path():
    spec = update_spec()
    assert spec.lock_path.name == "update.lock"
    assert spec.state_dir.name == ".stalwart-easy-deploy"
    assert "stalwart-easy-deploy" in spec.compose_projects


def test_main_passes_force_and_skip_git(monkeypatch):
    seen: dict = {}

    def fake_run(spec, **kwargs):
        seen["spec"] = spec
        seen.update(kwargs)
        return "skipped"

    monkeypatch.setattr("scripts.update.run_standard_update", fake_run)
    monkeypatch.setattr(sys, "argv", ["update", "--force", "--skip-git", "--skip-pull"])
    main()
    assert seen["force"] is True
    assert seen["skip_git"] is True
    assert seen["skip_pull"] is True
    assert seen["extra_apply_args"] == ["--skip-pull"]
