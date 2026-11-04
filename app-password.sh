#!/usr/bin/env bash
# Create a Stalwart app password for IMAP/SMTP (phones, Thunderbird, …).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"
cd "${SCRIPT_DIR}"

clear_parent_python_env
ensure_docker_group_session "$@"

exec uv run python -m scripts.app_password "$@"
