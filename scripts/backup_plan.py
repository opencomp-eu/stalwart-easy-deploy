#!/usr/bin/env python3
"""Dynamic backup plan for stalwart-easy-deploy (shared backup contract v1).

easydeploy-lib's shared loader (python/backup_plan.py) discovers this module and
calls resolve_plan(project_root). Data paths follow deploy.yaml so backups track
operator-customized data directories. Stalwart and Bulwark use bind mounts only:
no named volumes, no databases.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "easydeploy-lib" / "python"))

import yaml  # noqa: E402

STATE_DIR = ".stalwart-easy-deploy"
DEFAULT_STALWART_DATA_DIR = "/var/lib/stalwart"
DEFAULT_BULWARK_DATA_DIR = "/var/lib/bulwark"


def _load_deploy_config(project_root: Path) -> dict[str, Any]:
    deploy_yaml = project_root / "deploy.yaml"
    if not deploy_yaml.is_file():
        return {}
    data = yaml.safe_load(deploy_yaml.read_text()) or {}
    return data if isinstance(data, dict) else {}


def _bulwark_enabled(config: dict[str, Any]) -> bool:
    """Same semantics as scripts/apply.py::bulwark_enabled — absent means on."""
    bulwark = config.get("bulwark") or {}
    value = bulwark.get("enabled", True)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def resolve_plan(project_root: Path) -> dict[str, Any]:
    project_root = Path(project_root)
    config = _load_deploy_config(project_root)
    stalwart = config.get("stalwart") or {}
    bulwark = config.get("bulwark") or {}

    persistent_paths: list[dict[str, str]] = [
        {"path": "deploy.yaml", "as": "deploy.yaml"},
        {"path": STATE_DIR, "as": STATE_DIR},
        {
            "path": str(stalwart.get("data_dir") or DEFAULT_STALWART_DATA_DIR),
            "as": "data/stalwart",
        },
    ]
    if _bulwark_enabled(config):
        persistent_paths.append(
            {
                "path": str(bulwark.get("data_dir") or DEFAULT_BULWARK_DATA_DIR),
                "as": "data/bulwark",
            }
        )

    return {
        "service": "stalwart",
        "archive_prefix": "stalwart",
        "timer_name": "stalwart-easy-deploy-backup",
        "state_dir": STATE_DIR,
        "secrets_file": f"{STATE_DIR}/secrets.yaml",
        "hooks": {"apply": "apply.sh", "start": "start.sh", "stop": "stop.sh"},
        "persistent_paths": persistent_paths,
        "docker_volumes": [],
        "databases": [],
    }
