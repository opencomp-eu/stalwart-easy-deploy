#!/usr/bin/env python3
"""Read and write deploy.yaml for the Stalwart wizard."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEPLOY_PATH = PROJECT_ROOT / "deploy.yaml"


def load_or_init(path: Path = DEFAULT_DEPLOY_PATH) -> dict:
    if not path.exists():
        example = PROJECT_ROOT / "deploy.yaml.example"
        if example.is_file():
            with example.open() as handle:
                return yaml.safe_load(handle) or {}
        return {}

    with path.open() as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("deploy.yaml root must be a mapping")
    return data


def save(path: Path, data: dict) -> None:
    with path.open("w") as handle:
        yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=False)


def update_from_wizard(
    *,
    hostname: str,
    domain: str,
    data_dir: str,
    bulwark_enabled: bool,
    bulwark_domain: str,
    bulwark_data_dir: str,
    recovery_admin_password: str | None,
    proxy_mode: str,
    path: Path = DEFAULT_DEPLOY_PATH,
) -> None:
    config = load_or_init(path)

    stalwart = config.setdefault("stalwart", {})
    stalwart["hostname"] = hostname
    stalwart["domain"] = domain
    stalwart.setdefault("image", "docker.io/stalwartlabs/stalwart")
    stalwart.setdefault("tag", "v0.16")
    stalwart["data_dir"] = data_dir.rstrip("/")
    if recovery_admin_password:
        stalwart["recovery_admin_password"] = recovery_admin_password
    else:
        stalwart.pop("recovery_admin_password", None)

    bulwark: dict[str, Any] = config.setdefault("bulwark", {})
    bulwark["enabled"] = bulwark_enabled
    bulwark["domain"] = bulwark_domain
    bulwark.setdefault("image", "ghcr.io/bulwarkmail/webmail")
    bulwark.setdefault("tag", "1.7.5")
    bulwark["data_dir"] = bulwark_data_dir.rstrip("/")
    bulwark.setdefault("app_name", "Webmail")

    ports = config.setdefault("ports", {})
    ports.setdefault("smtp", 25)
    ports.setdefault("submission", 587)
    ports.setdefault("submissions", 465)
    ports.setdefault("imaps", 993)
    ports.setdefault("sieve", 4190)

    config["proxy"] = {
        "type": "caddy",
        "mode": proxy_mode,
        "integrate": {"network": "easydeploy-net"},
    }

    save(path, config)


def main() -> None:
    parser = argparse.ArgumentParser(description="Update deploy.yaml from wizard")
    parser.add_argument("--deploy-yaml", type=Path, default=DEFAULT_DEPLOY_PATH)
    args = parser.parse_args()
    if not args.deploy_yaml.exists():
        raise SystemExit(f"Missing {args.deploy_yaml}")


if __name__ == "__main__":
    main()
