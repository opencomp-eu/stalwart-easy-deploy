#!/usr/bin/env python3
"""Pull this kit, skip if unchanged, otherwise apply."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "easydeploy-lib" / "python"))
from update_lock import (  # noqa: E402
    UpdateSpec,
    add_update_arguments,
    integration_config_paths,
    record_update_lock,
    run_standard_update,
)


def update_spec(project_root: Path | None = None) -> UpdateSpec:
    root = project_root or PROJECT_ROOT
    state_dir = root / ".stalwart-easy-deploy"
    return UpdateSpec(
        project_root=root,
        state_dir=state_dir,
        config_paths=(root / "deploy.yaml", *integration_config_paths(state_dir)),
        compose_projects=("stalwart-easy-deploy",),
        extra_containers=("stalwart", "bulwark", "stalwart_caddy"),
    )


def record_current_lock(project_root: Path | None = None) -> None:
    record_update_lock(update_spec(project_root))


def main() -> None:
    parser = argparse.ArgumentParser(description="Update stalwart-easy-deploy")
    add_update_arguments(parser)
    args = parser.parse_args()
    extra = ["--skip-pull"] if args.skip_pull else []
    try:
        run_standard_update(
            update_spec(),
            force=args.force,
            skip_git=args.skip_git,
            skip_pull=args.skip_pull,
            verbose=args.verbose,
            extra_apply_args=extra,
        )
    except (FileNotFoundError, ValueError, RuntimeError, PermissionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
