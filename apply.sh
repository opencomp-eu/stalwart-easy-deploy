#!/usr/bin/env bash
# apply.sh — Apply configuration from deploy.yaml
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"
cd "${SCRIPT_DIR}"

clear_parent_python_env
ensure_docker_group_session "$@"

ensure_dependencies="false"
python_args=()

for arg in "$@"; do
	case "$arg" in
		--ensure-dependencies)
			ensure_dependencies="true"
			;;
		*)
			python_args+=("$arg")
			;;
	esac
done

if [[ "$ensure_dependencies" == "true" ]]; then
	bash "${SCRIPT_DIR}/ensure-dependencies.sh"
fi

exec uv run python -m scripts.apply "${python_args[@]}"
